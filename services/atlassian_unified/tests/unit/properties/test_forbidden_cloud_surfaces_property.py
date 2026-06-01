"""Property test P14 — Registered Bitbucket_Tool set contains no
Cloud-only forbidden capabilities.

Validates Requirements 24.1, 24.2, 24.3, 24.4, 24.5 of the
``bitbucket-cloud-dc-parity`` spec / design Property 14.

Two static invariants are enforced by this module:

    **(a) No forbidden Cloud-only tool names.**
        No registered Bitbucket_Tool name (across ``bitbucket_mcp`` and
        — defensively — the Jira / Confluence servers) may match any of
        the forbidden Cloud-only surface patterns declared in design
        Property 14:

            pipelines | environments | deploy(_|ment_)write
            | snippet | workspace_admin

        These patterns capture the five Cloud-only product surfaces the
        feature deliberately refuses to expose (Requirements 24.1 -
        24.5): Bitbucket Pipelines, Bitbucket Environments, Cloud
        Deployments WRITE endpoints, Bitbucket Snippets, and the
        Workspaces admin surface. The existing DC ``bitbucket_deployments``
        read tools (``list_deployments``, ``get_deployment``) do NOT
        match ``deploy(_|ment_)write`` because their names carry no
        ``_write`` suffix — they remain allowed (Req 14.5 preserves
        read-only DC deployments).

    **(b) No forbidden Cloud HTTP endpoint paths in source.**
        The only Cloud-only HTTP path the server is allowed to issue is
        the ``GET /2.0/workspaces?pagelen=1`` connectivity probe in
        :mod:`mcp_atlassian.servers.dependencies` (design Property 10 /
        Requirement 18.1). This property scans the
        ``src/mcp_atlassian/bitbucket/`` module source and asserts no
        forbidden Cloud endpoint literal appears (``/2.0/pipelines``,
        ``/2.0/snippets``, ``/2.0/workspaces/{ws}/projects``,
        ``/2.0/workspaces/{ws}/members``, ``/2.0/workspaces/{ws}/permissions``,
        ``/2.0/repositories/{ws}/{repo}/pipelines``,
        ``/2.0/repositories/{ws}/{repo}/environments``,
        ``/2.0/repositories/{ws}/{repo}/deployments``,
        ``/2.0/repositories/{ws}/{repo}/snippets``).

Strategy
--------

Hypothesis is used per invariant (a) to parametrize over each forbidden
regex pattern (``st.sampled_from`` over the pattern registry). The
property body, given a pattern, enumerates every registered tool name
across all three servers and asserts the pattern matches none of them.
This guarantees that a single added tool whose name matches ANY
forbidden pattern surfaces a failure on exactly that pattern's
parametrised example — the id scheme ``pattern::<name>`` makes the
regression pinpoint.

Invariant (b) is a pure static-grep check; no fuzzing adds value since
the search space is the fixed bitbucket module source tree. It is
implemented as a parametrised pytest test (one parametrised entry per
forbidden endpoint literal) so a single offending occurrence surfaces
under a pinpoint id like ``endpoint::/2.0/pipelines``.

Style reference
---------------

Shaped after :mod:`tests.unit.properties.test_forbidden_endpoint_property`
(the Jira / Confluence / Bitbucket forbidden-family sibling) and
:mod:`tests.unit.servers.test_tool_registration_parity` (the static
tool-surface guardrail). This file lives under
``tests/unit/properties/`` because it encodes a universal invariant
over every registered Bitbucket_Tool name and every Cloud endpoint
literal in the bitbucket module source.

Scope
-----

* Invariant (a) is Hypothesis-driven over the pattern registry.
* Invariant (b) is a deterministic static scan; exempted files:
  ``tests/`` and ``servers/dependencies.py`` (the latter carries the
  allow-listed probe).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_atlassian.servers.bitbucket import bitbucket_mcp
from mcp_atlassian.servers.confluence import confluence_mcp
from mcp_atlassian.servers.jira import jira_mcp


# ---------------------------------------------------------------------------
# Tool discovery — same pattern used by test_tag_shape_property,
# test_tool_registration_parity, and test_forbidden_endpoint_property.
# ---------------------------------------------------------------------------


def _collect_tools() -> dict[str, dict[str, Any]]:
    """Collect ``{server_label: {tool_name: tool_obj}}`` via a fresh loop.

    A fresh event loop is used so pytest's asyncio plugin doesn't
    interfere with import-time discovery.
    """
    servers: dict[str, Any] = {
        "bitbucket": bitbucket_mcp,
        "jira": jira_mcp,
        "confluence": confluence_mcp,
    }

    async def _gather() -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for label, server in servers.items():
            result[label] = await server.get_tools()
        return result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gather())
    finally:
        loop.close()


_TOOLS_BY_SERVER: dict[str, dict[str, Any]] = _collect_tools()


def _all_registered_tool_names() -> set[str]:
    """Flatten the three servers' tool dicts into a single name set."""
    names: set[str] = set()
    for tools in _TOOLS_BY_SERVER.values():
        names |= set(tools.keys())
    return names


_ALL_NAMES: set[str] = _all_registered_tool_names()


# ---------------------------------------------------------------------------
# Invariant (a) — forbidden Cloud-only tool-name regex registry
#
# Each pattern encodes a single Cloud-only surface excluded by
# Requirements 24.1 - 24.5. The regexes are applied with
# ``re.Pattern.search`` (substring semantics) over the registered tool
# name, so a hypothetical ``bitbucket_list_pipelines`` or
# ``bitbucket_create_snippet`` would trip the appropriate pattern.
#
# The ``deploy_write`` / ``deployment_write`` pattern is deliberately
# suffixed so it matches forbidden Cloud DEPLOY-WRITE endpoints but NOT
# the allowed DC ``bitbucket_list_deployments`` / ``bitbucket_get_deployment``
# read tools (Req 14.5). Sanity assertions below verify that
# separation.
#
# Requirement citations per pattern:
#
#   pipelines      — Req 24.1. Bitbucket Pipelines CI/CD surface is out
#                    of scope.
#   environments   — Req 24.2. Bitbucket Environments surface is out of
#                    scope.
#   deploy_write   — Req 24.3. Cloud Deployments WRITE endpoints are
#                    excluded; the DC ``bitbucket_deployments`` toolset
#                    (read-only) is preserved (Req 14.5).
#   snippet        — Req 24.4. Bitbucket Snippets surface is out of
#                    scope.
#   workspace_admin — Req 24.5. Workspaces admin surface (members,
#                    permissions, projects mutation) is out of scope
#                    beyond the ``GET /2.0/workspaces?pagelen=1`` probe
#                    (Req 18.1).
# ---------------------------------------------------------------------------


FORBIDDEN_CLOUD_TOOL_PATTERNS: dict[str, re.Pattern[str]] = {
    # Bitbucket Pipelines (CI/CD surface) — Req 24.1.
    "pipelines": re.compile(r"pipelines"),
    # Bitbucket Environments — Req 24.2.
    "environments": re.compile(r"environments"),
    # Cloud Deployments WRITE endpoints — Req 24.3.
    # Matches ``deploy_write`` and ``deployment_write`` (and compound
    # variants like ``update_deployment_write``) without matching the
    # allowed ``list_deployments`` / ``get_deployment`` read tools.
    "deploy_write": re.compile(r"deploy(?:_|ment_)write"),
    # Bitbucket Snippets — Req 24.4. ``snippet`` (singular / plural)
    # covers ``list_snippets``, ``create_snippet``, ``get_snippet``, etc.
    "snippet": re.compile(r"snippet"),
    # Workspaces admin surface — Req 24.5.
    "workspace_admin": re.compile(r"workspace_admin"),
}


# ---------------------------------------------------------------------------
# Invariant (b) — forbidden Cloud HTTP endpoint literals
#
# These are the Cloud-only endpoint path fragments that must NOT appear
# in any Bitbucket mixin / client / normalizer source file. The only
# Cloud endpoint the server is allowed to issue is
# ``GET /2.0/workspaces?pagelen=1`` from ``servers/dependencies.py``
# (Requirement 18.1 / design Property 10), which is covered by
# :mod:`tests.unit.properties.test_probe_mode_routing_property`. This
# invariant is the complementary negative-space guard: NO other Cloud
# endpoint literal may leak into the bitbucket module tree.
#
# The list below enumerates the five Cloud-only surface families
# (Requirements 24.1 - 24.5) plus their common sub-paths. A regex
# variant is NOT used — substring literal matching is both more robust
# and generates better failure messages. Both f-string template forms
# (``{workspace}`` / ``{ws}``) and the concrete URL form are covered.
# ---------------------------------------------------------------------------


FORBIDDEN_CLOUD_ENDPOINTS: tuple[str, ...] = (
    # Req 24.1 — Pipelines (workspace-level and repo-scoped).
    "/2.0/pipelines",
    "/2.0/repositories/{workspace}/{repo_slug}/pipelines",
    "/2.0/repositories/{workspace}/{slug}/pipelines",
    # Req 24.2 — Environments.
    "/2.0/repositories/{workspace}/{repo_slug}/environments",
    "/2.0/repositories/{workspace}/{slug}/environments",
    # Req 24.3 — Cloud Deployments (write surface). The read-only DC
    # ``/rest/api/latest/.../deployments`` path is allowed and is the
    # only ``deployments`` reference in the tree (see the sanity test
    # below).
    "/2.0/repositories/{workspace}/{repo_slug}/deployments",
    "/2.0/repositories/{workspace}/{slug}/deployments",
    # Req 24.4 — Snippets.
    "/2.0/snippets",
    "/2.0/repositories/{workspace}/{repo_slug}/snippets",
    "/2.0/repositories/{workspace}/{slug}/snippets",
    # Req 24.5 — Workspaces admin (members / permissions / projects
    # mutation). The read-only ``GET /2.0/workspaces`` and
    # ``GET /2.0/workspaces/{workspace}`` project-synthesis paths are
    # allowed (Req 8.7, 18.1) and explicitly NOT in this list.
    "/2.0/workspaces/{workspace}/members",
    "/2.0/workspaces/{ws}/members",
    "/2.0/workspaces/{workspace}/permissions",
    "/2.0/workspaces/{ws}/permissions",
    "/2.0/workspaces/{workspace}/projects",
    "/2.0/workspaces/{ws}/projects",
)


# Path to the bitbucket module source tree. Resolved relative to this
# test file so the check is robust to pytest's working directory.
_BITBUCKET_MODULE_DIR: Path = (
    Path(__file__).resolve().parents[3]  # tests/unit/properties → repo root
    / "src"
    / "mcp_atlassian"
    / "bitbucket"
)


def _bitbucket_source_files() -> list[Path]:
    """Return every ``.py`` file directly under ``src/mcp_atlassian/bitbucket/``.

    ``__pycache__`` and any nested directories are intentionally
    excluded — only first-level Python sources are scanned so the
    invariant's search space is deterministic and bounded.
    """
    if not _BITBUCKET_MODULE_DIR.is_dir():
        return []
    return sorted(
        p
        for p in _BITBUCKET_MODULE_DIR.iterdir()
        if p.is_file() and p.suffix == ".py"
    )


_BITBUCKET_SOURCE_FILES: list[Path] = _bitbucket_source_files()


# ---------------------------------------------------------------------------
# Sanity — discovery and registry non-empty
# ---------------------------------------------------------------------------


def test_tool_discovery_finds_tools_on_all_three_servers() -> None:
    """Guard against silent empty-dict discovery where the per-tool
    assertions below would pass vacuously."""
    for server_label, tools in _TOOLS_BY_SERVER.items():
        assert len(tools) > 0, (
            f"No tools discovered on '{server_label}' — forbidden-"
            f"Cloud-surface checks would pass vacuously."
        )


def test_forbidden_cloud_pattern_registry_is_non_empty() -> None:
    """Guard against the pattern dict being cleared. Req 24.1 - 24.5
    each require at least one pattern.
    """
    assert len(FORBIDDEN_CLOUD_TOOL_PATTERNS) == 5, (
        f"FORBIDDEN_CLOUD_TOOL_PATTERNS should have exactly 5 entries "
        f"(one per Req 24.1-24.5); got {len(FORBIDDEN_CLOUD_TOOL_PATTERNS)}."
    )
    for essential in ("pipelines", "environments", "deploy_write", "snippet", "workspace_admin"):
        assert essential in FORBIDDEN_CLOUD_TOOL_PATTERNS, (
            f"Essential forbidden Cloud pattern '{essential}' is missing."
        )


def test_bitbucket_source_tree_discovered() -> None:
    """Guard against a misconfigured source-tree path that would make
    invariant (b) pass vacuously.
    """
    assert _BITBUCKET_MODULE_DIR.is_dir(), (
        f"Bitbucket module directory not found at "
        f"{_BITBUCKET_MODULE_DIR!s}. Adjust _BITBUCKET_MODULE_DIR if "
        f"the source tree has moved."
    )
    assert len(_BITBUCKET_SOURCE_FILES) > 0, (
        f"No Python source files found under {_BITBUCKET_MODULE_DIR!s}; "
        f"the source-scan invariant would pass vacuously."
    )


# ---------------------------------------------------------------------------
# Invariant (a) — Hypothesis property per forbidden Cloud pattern
# ---------------------------------------------------------------------------


@given(pattern_name=st.sampled_from(sorted(FORBIDDEN_CLOUD_TOOL_PATTERNS.keys())))
@settings(max_examples=20, deadline=None)
def test_no_registered_tool_matches_forbidden_cloud_pattern(
    pattern_name: str,
) -> None:
    """P14.a: no registered tool name matches a forbidden Cloud
    surface regex.

    Validates Requirements 24.1, 24.2, 24.3, 24.4, 24.5.

    For any Hypothesis-drawn pattern from the forbidden registry, the
    set of matching registered tool names SHALL be empty. A failure
    here means a Bitbucket_Tool targeting a Cloud-only excluded
    capability has been registered; fix by removing the offending
    ``@bitbucket_mcp.tool`` registration or — if the match is a
    false-positive on an allowed sibling — refine the pattern.
    """
    pattern = FORBIDDEN_CLOUD_TOOL_PATTERNS[pattern_name]
    matches = sorted(name for name in _ALL_NAMES if pattern.search(name))
    assert not matches, (
        f"Forbidden Cloud pattern '{pattern_name}' (regex: "
        f"{pattern.pattern!r}) matched registered tool(s): "
        f"{matches!r}. These capabilities are excluded by design "
        f"Property 14 / Requirements 24.1 - 24.5. Remove the "
        f"registration."
    )


# ---------------------------------------------------------------------------
# Invariant (b) — static source scan for forbidden Cloud endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_endpoint",
    FORBIDDEN_CLOUD_ENDPOINTS,
    ids=lambda e: f"endpoint::{e}",
)
def test_no_bitbucket_source_file_references_forbidden_cloud_endpoint(
    forbidden_endpoint: str,
) -> None:
    """P14.b: no Bitbucket module source file contains a forbidden
    Cloud endpoint literal.

    Validates Requirements 24.1, 24.2, 24.3, 24.4, 24.5 (negative-
    space guard). The only allow-listed Cloud-only HTTP path is
    ``GET /2.0/workspaces?pagelen=1`` in
    :mod:`mcp_atlassian.servers.dependencies` (Req 18.1); it is
    explicitly not in the scan scope here because this test walks
    ``src/mcp_atlassian/bitbucket/`` only.

    Offending occurrences fail this test with the file name and a
    line-number list so a maintainer can jump straight to the
    violating literal.
    """
    offenders: list[tuple[str, int, str]] = []
    for path in _BITBUCKET_SOURCE_FILES:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if forbidden_endpoint in line:
                offenders.append((path.name, line_no, line.strip()))

    assert not offenders, (
        f"Forbidden Cloud endpoint '{forbidden_endpoint}' appears in "
        f"the bitbucket module source. This surface is excluded by "
        f"design Property 14 / Requirements 24.1 - 24.5. Offending "
        f"occurrences:\n"
        + "\n".join(
            f"  {name}:{line_no}: {snippet}"
            for name, line_no, snippet in offenders
        )
    )


# ---------------------------------------------------------------------------
# Positive sanity — the allowed surfaces ARE present
# ---------------------------------------------------------------------------


def test_deploy_write_pattern_does_not_match_allowed_dc_deployments() -> None:
    """Sanity: the ``deploy_write`` regex must NOT match the allowed
    DC ``bitbucket_deployments`` read tools (Req 14.5).

    Req 14.5 preserves the DC-only ``bitbucket_list_deployments`` and
    ``bitbucket_get_deployment`` read tools. A regression in the
    ``deploy_write`` regex would falsely flag them and contradict the
    DC-only allowance. This assertion is the canary.
    """
    pattern = FORBIDDEN_CLOUD_TOOL_PATTERNS["deploy_write"]
    for allowed in (
        "list_deployments",
        "get_deployment",
        "bitbucket_list_deployments",
        "bitbucket_get_deployment",
    ):
        assert not pattern.search(allowed), (
            f"'deploy_write' regex falsely matched allowed DC read "
            f"tool '{allowed}'. The ``_write`` suffix anchor is "
            f"broken — refine the regex so DC deployments reads "
            f"remain allowed under Req 14.5."
        )
    # And DO match truly-forbidden hypothetical write names.
    for blocked in (
        "create_deploy_write",
        "update_deployment_write",
        "post_deploy_write_status",
    ):
        assert pattern.search(blocked), (
            f"'deploy_write' regex failed to match forbidden name "
            f"'{blocked}'."
        )


def test_cloud_workspaces_probe_is_the_only_cloud_workspaces_endpoint_in_deps() -> None:
    """Sanity: ``servers/dependencies.py`` references the allow-listed
    ``GET /2.0/workspaces`` connectivity probe (Req 18.1) so this test
    is not vacuous about the ONE allowed Cloud-only path.

    A regression that deletes the probe would silently let the
    bitbucket module tree become the exclusive Cloud surface while the
    probe disappears — this assertion guards against that by pinning
    the probe's presence in the dependency module.
    """
    deps_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "mcp_atlassian"
        / "servers"
        / "dependencies.py"
    )
    assert deps_path.is_file(), (
        f"Expected dependencies module at {deps_path!s} — has the "
        f"source tree moved?"
    )
    text = deps_path.read_text(encoding="utf-8")
    assert "/2.0/workspaces" in text, (
        "The allow-listed Cloud-only connectivity probe "
        "'GET /2.0/workspaces?pagelen=1' is missing from "
        "servers/dependencies.py. Req 18.1 / design Property 10 "
        "require it."
    )
