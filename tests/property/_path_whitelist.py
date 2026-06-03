"""Helper module: AST-based path-whitelist scanner.



This module is a *reusable* scanning library consumed by other invariant in this directory:

*:mod:`test_path_coverage` (invariant — sensitive call path whitelist)
*:mod:`test_llm_call_paths` (LLM-specific subset)
*:mod:`test_workflow_determinism_static` (already in-tree; this module
 augments it with the ``client.start_workflow`` activity check)

The module exports four public scan functions plus the underlying data
classes. Each scan walks a Python source tree (anchored on the platform
root by default), parses every ``*.py`` file with:func:`ast.parse`, and
returns a list of:class:`Finding` instances reporting the offending
file path, line number, and a short symbolic key naming the violation.

Scanners
--------

*:func:`scan_atlassian_http_calls` — Jira / Bitbucket / Confluence
 hosts reached via ``httpx`` / ``requests`` / ``aiohttp`` outside the
 ``atlassian_mcp_bitbucket`` MCP and the ``http-shared`` lib. The scan
 recognises both module-level imports (``import httpx`` /
 ``from httpx import...``) and attribute call targets (``httpx.AsyncClient(...)``,
 ``requests.get(...)``, ``aiohttp.ClientSession(...)``) inside files
 whose source mentions an Atlassian host (``atlassian.net``,
 ``atlassian-mcp``, ``bitbucket.org``, ``api.bitbucket.org``,
 ``confluence``) — the host literal is the second hop that
 distinguishes a generic HTTP client from an Atlassian-specific call.

*:func:`scan_ssh_docker_calls` — ``paramiko`` / ``asyncssh`` imports
 and ``subprocess`` calls whose first positional argument starts with
 ``ssh`` or ``scp`` — SSH only inside
 ``execution-runner-worker``).

*:func:`scan_llm_calls` — ``openai`` / ``anthropic`` imports and
 imports of the ``llm_orchestrator`` library — LLM
 only inside ``assistant-service`` and ``agent-runner-worker``).

*:func:`scan_activities_start_workflow` — searches files under
 ``workers/*/activities/`` for ``client.start_workflow``-shaped calls
 — workflow decisions live in workflow modules only).

Each scanner accepts a tuple of *whitelist roots* (workspace-relative
path prefixes) and returns only:class:`Finding` instances whose file
path is **outside** every whitelisted root. Files inside a whitelisted
root are inspected but never reported.

Design notes
------------

* The scanner is *static* — it never imports the inspected modules,
 so a missing transitive dependency in the workspace does not skew
 results. The same approach is used by:mod:`test_workflow_determinism_static` and is documented in design
 §6.3 (invariant → Test mapping).
* Files under standard exclusion roots (``__pycache__``, ``.venv``,
 ``.pytest_cache``, ``.hypothesis``, ``.mypy_cache``, ``.ruff_cache``,
 ``node_modules``, ``dist``, ``build``, ``.git``, ``.next``) and the
 ``services/atlassian_mcp_bitbucket/`` gateway subtree are skipped at the
 walk level. The:data:`SCAN_EXCLUDED_DIRS` constant is exported so
 downstream tests can extend it.
* A file's *own tests* (``tests/`` subtree under any component path)
 is included by default because the same invariants must hold in
 test source as in production source — for example, an integration
 test that smuggles ``import paramiko`` outside the execution-runner
 worker would defeat the property.

This module ships *no* test functions; importing it has no side
effects beyond resolving the platform root path.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# ---------------------------------------------------------------------------
# Workspace anchors
# ---------------------------------------------------------------------------

# tests/property/_path_whitelist.py → platform/
PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]

# Directory names pruned from every os.walk used by the scanners. Tooling
# caches and vendored trees are excluded so the scan stays bounded to
# the workspace's own source. ``services/atlassian_mcp_bitbucket`` is
# excluded globally because it legitimately contains Atlassian HTTP calls.
SCAN_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        ".git",
        ".next",
        ".hypothesis",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
        "atlassian_mcp_bitbucket",
    }
)


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A single violation reported by one of the scanners.

 Attributes
 ----------
 path:
 Workspace-relative file path (forward-slash normalised) where
 the offending node was discovered.
 lineno:
 1-indexed source line of the offending AST node.
 category:
 One of ``"atlassian_http"``, ``"ssh"``, ``"docker"``, ``"llm"``,
 ``"activity_start_workflow"`` - names the scan that produced
 the finding.
 symbol:
 Dotted symbol or short tag describing the violation
 (e.g. ``"httpx.AsyncClient"``, ``"paramiko"``,
 ``"openai"``, ``"client.start_workflow"``).
 detail:
 Free-form human description used in error messages by the
 consuming invariant.
 """

    path: str
    lineno: int
    category: str
    symbol: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Default whitelist roots (per the operational rule)
# ---------------------------------------------------------------------------

# Workspace-relative path prefixes (forward-slash). A finding is
# suppressed if its file path begins with any of these prefixes.
#
# The defaults below mirror the the operational rule language verbatim:
#
# * ATLASSIAN_HTTP_WHITELIST — Atlassian HTTP only
# through the ``atlassian_mcp_bitbucket`` MCP. The MCP itself is excluded
# at the walk level via SCAN_EXCLUDED_DIRS, but the ``http-shared``
# library is the legitimate transport for callers (it builds the
# ``httpx`` client wired to the MCP) and the ``mcp_client`` lib (when
# it is wired) is the shared caller-side source.
# * SSH_DOCKER_WHITELIST —: SSH and Docker only in the
# ``execution-runner-worker``.
# * LLM_WHITELIST —: LLM calls only in the
# ``assistant-service`` and ``agent-runner-worker`` (and the
# ``llm-orchestrator`` library that defines them).

ATLASSIAN_HTTP_WHITELIST: tuple[str, ...] = (
    "services/atlassian_mcp_bitbucket/",
    "libs/http-shared/",
    "libs/mcp_client/",
)

SSH_DOCKER_WHITELIST: tuple[str, ...] = (
    "workers/execution-runner-worker/",
)

LLM_WHITELIST: tuple[str, ...] = (
    "services/assistant-service/",
    "workers/agent-runner-worker/",
    "libs/llm-orchestrator/",
)

# Roots that always permit *test fixtures* — these are pure test projects
# and never run in production. We allow violations in shared
# ``tests/property/`` _scanners_ themselves (this module + sibling
# invariant) because they reference the symbols by name in
# strings/AST literals, not as live calls. The scan still excludes
# files by path; we do *not* exclude any worker/service ``tests/``
# subtrees because those reflect production import-graph correctness.
SHARED_TEST_FIXTURE_WHITELIST: tuple[str, ...] = (
    "tests/property/",
    "tests/integration/",
    "tests/unit/",
    "tests/fixtures/",
    "tests/conftest.py",
)


# ---------------------------------------------------------------------------
# Source corpus iteration
# ---------------------------------------------------------------------------


def _normalise(rel: Path) -> str:
    """Return *rel* as a forward-slash workspace-relative string."""

    return rel.as_posix()


def iter_source_files(
    root: Path = PLATFORM_ROOT,
    *,
    excluded_dirs: Iterable[str] = SCAN_EXCLUDED_DIRS,
) -> Iterator[Path]:
    """Yield every ``*.py`` file under *root*, pruning excluded dirs.

 The walk mutates the ``dirnames`` list returned by:func:`os.walk`
 so excluded subtrees are not descended into. Yields absolute paths
 in deterministic (sorted) order per directory.
 """

    excluded = frozenset(excluded_dirs)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def _is_under(rel_posix: str, prefixes: Sequence[str]) -> bool:
    """Return True if *rel_posix* starts with any prefix in *prefixes*.

 *rel_posix* is expected to be forward-slash-normalised. Prefixes
 are compared with a trailing slash so ``services/foo`` does not
 match ``services/foo-bar/``.
 """

    for prefix in prefixes:
        normalised = prefix if prefix.endswith("/") else prefix + "/"
        # Allow exact-file matches (e.g. tests/conftest.py).
        if rel_posix == prefix.rstrip("/"):
            return True
        if rel_posix.startswith(normalised):
            return True
    return False


def _parse(path: Path) -> ast.Module | None:
    """Parse *path* into an AST module, returning None on syntax error.

 A syntax error in a non-whitelisted file should not crash the
 scanner — the file is simply skipped and the caller can choose to
 surface it via a separate parse-validation test.
 """

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _dotted_name(node: ast.expr) -> str | None:
    """Return the dotted attribute chain rooted at a Name, or None.

 Mirrors the helper used by ``test_workflow_determinism_static`` so
 behaviour stays consistent across scanners.
 """

    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _imported_modules(tree: ast.Module) -> set[str]:
    """Return the set of top-level module names imported by *tree*.

 Captures both ``import x.y`` (yields ``"x"``) and ``from x.y import
 z`` (yields ``"x"``). Relative imports (``from. import...``) are
 ignored — they cannot reach a third-party package by definition.
 """

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module.split(".", 1)[0])
    return modules


# ---------------------------------------------------------------------------
# Atlassian host detection
# ---------------------------------------------------------------------------


# Host substrings that mark a request as **direct upstream Atlassian**.
# The match is *substring* (case-insensitive) against any string
# literal in the file's AST, so URL building styles
# (``f"https://{host}/..."``, ``base + "/rest/..."``) are caught as
# long as the literal host appears somewhere in source.
#
# Important: ``atlassian-mcp`` (the MCP proxy hostname Compose service
# at ``http://atlassian-mcp:8090``) is **NOT** on this list. The MCP is
# the *allowed* path for every Atlassian call — forbids
# only direct upstream calls that bypass the MCP. Files that use
# ``httpx`` to talk to ``atlassian-mcp`` are therefore not flagged.
#
# Patterns are intentionally narrow: each entry is a URL fragment that
# unambiguously identifies a real Atlassian upstream (``*.atlassian.net``
# Cloud sites, ``*.bitbucket.org`` Cloud sites, REST API path prefixes
# that only exist on the upstream APIs). Bare words like ``"jira"``,
# ``"bitbucket"``, or ``"confluence"`` are deliberately *not* listed —
# those appear in docstrings, dataclass names, log messages, and
# fixture identifiers and would produce false positives.
ATLASSIAN_HOST_PATTERNS: tuple[str, ...] = (
    ".atlassian.net",       # Cloud sites: <tenant>.atlassian.net
    ".atlassian.com",       # Atlassian-owned domains (rare in code)
    ".bitbucket.org",       # Bitbucket Cloud
    "api.bitbucket.org",    # Bitbucket Cloud REST
    "/rest/api/3/",         # Jira REST v3 (Cloud)
    "/rest/api/2/",         # Jira REST v2 (Server / DC)
    "/wiki/rest/api/",      # Confluence Cloud REST
    "/2.0/repositories/",   # Bitbucket Cloud REST
    "/2.0/workspaces/",     # Bitbucket Cloud REST
)

# HTTP client modules whose use against an Atlassian host is the target
# of.
ATLASSIAN_HTTP_MODULES: frozenset[str] = frozenset({"httpx", "requests", "aiohttp"})


def _file_mentions_atlassian_host(tree: ast.Module) -> bool:
    """Return True if any string literal in *tree* references an Atlassian host.

 The check runs over:class:`ast.Constant` nodes whose value is a
 string — this catches both bare URL literals and f-string fragments
 (Python lowers f-strings into ``JoinedStr`` containing ``Constant``
 leaves).
 """

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.lower()
            for pat in ATLASSIAN_HOST_PATTERNS:
                if pat.lower() in value:
                    return True
    return False


# ---------------------------------------------------------------------------
# Scan: Atlassian HTTP calls
# ---------------------------------------------------------------------------


def scan_atlassian_http_calls(
    root: Path = PLATFORM_ROOT,
    *,
    whitelist: Sequence[str] = ATLASSIAN_HTTP_WHITELIST,
) -> list[Finding]:
    """Find files reaching Atlassian hosts outside the MCP whitelist.



 A finding is reported when, in a single ``.py`` file outside the
 whitelist, **both** of the following hold:

 1. the file imports at least one of ``httpx`` / ``requests`` /
 ``aiohttp`` (or accesses one of those modules as a dotted call
 target), and
 2. the same file's source contains a string literal that matches
 any of:data:`ATLASSIAN_HOST_PATTERNS`.

 The two-condition gate is critical — generic HTTP clients are
 permitted everywhere, and Atlassian host literals appear in
 docstrings and tests. The conjunction (HTTP client + Atlassian
 host literal in the *same* file) is the signal that the operational rule is being violated.

 For each matching file the scanner reports the line of the *first*
 HTTP-client call site (or the ``import`` line if no call site is
 found) so error messages point at actionable code.
 """

    findings: list[Finding] = []
    for path in iter_source_files(root):
        rel = _normalise(path.relative_to(root))
        if _is_under(rel, whitelist):
            continue

        tree = _parse(path)
        if tree is None:
            continue

        modules = _imported_modules(tree)
        relevant = modules & ATLASSIAN_HTTP_MODULES
        if not relevant:
            continue

        if not _file_mentions_atlassian_host(tree):
            continue

        # Pick the line of the first HTTP-client AST node that we can
        # find: prefer a call-site Attribute (``httpx.AsyncClient``)
        # over the bare import.
        offending_lineno = 1
        offending_symbol = sorted(relevant)[0]
        first_call = _find_first_http_call_node(tree, ATLASSIAN_HTTP_MODULES)
        if first_call is not None:
            offending_lineno = first_call.lineno
            dotted = _dotted_name(first_call.func) if isinstance(
                first_call, ast.Call
            ) else None
            offending_symbol = dotted or offending_symbol

        findings.append(
            Finding(
                path=rel,
                lineno=offending_lineno,
                category="atlassian_http",
                symbol=offending_symbol,
                detail=(
                    "Atlassian host accessed via direct HTTP client outside "
                    "the atlassian_mcp_bitbucket MCP. Route every "
                    "Jira/Bitbucket/Confluence call through the MCP."
                ),
            )
        )

    return findings


def _find_first_http_call_node(
    tree: ast.Module, modules: frozenset[str]
) -> ast.Call | None:
    """Return the first:class:`ast.Call` whose target is rooted at one
 of *modules* (e.g. ``httpx.X(...)``, ``requests.get(...)``).

 Returns None if no such call exists in *tree*.
 """

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _dotted_name(node.func)
            if dotted is None:
                continue
            root = dotted.split(".", 1)[0]
            if root in modules:
                return node
    return None


# ---------------------------------------------------------------------------
# Scan: SSH / Docker calls
# ---------------------------------------------------------------------------


SSH_MODULES: frozenset[str] = frozenset({"paramiko", "asyncssh"})
DOCKER_MODULES: frozenset[str] = frozenset({"docker"})

# subprocess invocations whose first positional arg matches this regex
# are treated as SSH/SCP shell-out calls, second
# clause). The regex is anchored at the start of the literal so
# substrings like ``"my-ssh-helper"`` do not false-positive.
_SSH_LITERAL_RE = re.compile(r"^(ssh|scp)(\s|$)")


def scan_ssh_docker_calls(
    root: Path = PLATFORM_ROOT,
    *,
    whitelist: Sequence[str] = SSH_DOCKER_WHITELIST,
) -> list[Finding]:
    """Find SSH / Docker usage outside ``execution-runner-worker``.



 Three signal sources are considered:

 1. ``import paramiko`` / ``import asyncssh`` (or ``from`` variants)
 — reported as ``category="ssh"``, ``symbol="paramiko"`` etc.
 2. ``import docker`` (the Docker SDK) — reported as
 ``category="docker"``, ``symbol="docker"``.
 3. ``subprocess.run("ssh...")`` / ``subprocess.Popen(["scp",...])``
 — reported as ``category="ssh"``, ``symbol="subprocess+ssh"``.
 """

    findings: list[Finding] = []
    for path in iter_source_files(root):
        rel = _normalise(path.relative_to(root))
        if _is_under(rel, whitelist):
            continue

        tree = _parse(path)
        if tree is None:
            continue

        # Imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = (alias.name or "").split(".", 1)[0]
                    if root_mod in SSH_MODULES:
                        findings.append(
                            Finding(
                                path=rel,
                                lineno=node.lineno,
                                category="ssh",
                                symbol=root_mod,
                                detail=(
                                    f"SSH library {root_mod!r} imported "
                                    "outside execution-runner-worker. "
                                    "the operational rule."
                                ),
                            )
                        )
                    elif root_mod in DOCKER_MODULES:
                        findings.append(
                            Finding(
                                path=rel,
                                lineno=node.lineno,
                                category="docker",
                                symbol=root_mod,
                                detail=(
                                    "Docker SDK imported outside "
                                    "execution-runner-worker. "
                                    "the operational rule."
                                ),
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    root_mod = node.module.split(".", 1)[0]
                    if root_mod in SSH_MODULES:
                        findings.append(
                            Finding(
                                path=rel,
                                lineno=node.lineno,
                                category="ssh",
                                symbol=root_mod,
                                detail=(
                                    f"SSH library {root_mod!r} imported "
                                    "outside execution-runner-worker. "
                                    "the operational rule."
                                ),
                            )
                        )
                    elif root_mod in DOCKER_MODULES:
                        findings.append(
                            Finding(
                                path=rel,
                                lineno=node.lineno,
                                category="docker",
                                symbol=root_mod,
                                detail=(
                                    "Docker SDK imported outside "
                                    "execution-runner-worker. "
                                    "the operational rule."
                                ),
                            )
                        )

        # subprocess shell-out calls
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted_name(node.func)
            if dotted is None:
                continue
            if dotted in {
                "subprocess.run",
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "subprocess.Popen",
            }:
                if _subprocess_first_arg_is_ssh(node):
                    findings.append(
                        Finding(
                            path=rel,
                            lineno=node.lineno,
                            category="ssh",
                            symbol=f"subprocess+{_subprocess_command_name(node)}",
                            detail=(
                                "subprocess shell-out to ssh/scp outside "
                                "execution-runner-worker. the operational rule."
                            ),
                        )
                    )

    return findings


def _subprocess_first_arg(call: ast.Call) -> ast.expr | None:
    """Return the first positional arg of *call*, or None if absent."""

    if not call.args:
        return None
    return call.args[0]


def _string_literal(node: ast.expr) -> str | None:
    """Return the literal string value of *node*, or None if it is not
 a constant string."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _subprocess_first_arg_is_ssh(call: ast.Call) -> bool:
    """Return True if the first positional arg of *call* is an
 ``ssh``/``scp`` invocation.

 Two shapes are recognised:

 * ``subprocess.run("ssh user@host echo hi")`` — string literal
 whose first whitespace-separated token is ``ssh`` or ``scp``.
 * ``subprocess.run(["ssh", "user@host",...])`` — list literal
 whose first element is the string ``"ssh"`` or ``"scp"``.

 Anything dynamic (variables, ``shlex.split(...)``, formatted
 strings whose static prefix is not ``ssh``/``scp``) is treated as
 *not* an SSH call so the scan stays specific. Tests can extend
 coverage if needed.
 """

    arg = _subprocess_first_arg(call)
    if arg is None:
        return False

    literal = _string_literal(arg)
    if literal is not None:
        return bool(_SSH_LITERAL_RE.match(literal.lstrip()))

    if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
        first = _string_literal(arg.elts[0])
        if first is not None:
            return first in {"ssh", "scp"}
    return False


def _subprocess_command_name(call: ast.Call) -> str:
    """Return ``"ssh"`` or ``"scp"`` based on the first arg of *call*."""

    arg = _subprocess_first_arg(call)
    if arg is None:
        return "ssh"

    literal = _string_literal(arg)
    if literal is not None:
        token = literal.lstrip().split(None, 1)[0] if literal.strip() else "ssh"
        return token if token in {"ssh", "scp"} else "ssh"

    if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
        first = _string_literal(arg.elts[0])
        if first in {"ssh", "scp"}:
            return first
    return "ssh"


# ---------------------------------------------------------------------------
# Scan: LLM calls
# ---------------------------------------------------------------------------


# Top-level module names that mark a file as an LLM caller.
LLM_MODULES: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "llm_orchestrator",  # libs/llm-orchestrator dist name
    }
)


def scan_llm_calls(
    root: Path = PLATFORM_ROOT,
    *,
    whitelist: Sequence[str] = LLM_WHITELIST,
) -> list[Finding]:
    """Find LLM client usage outside the LLM-allowed components.



 The scanner reports any file outside *whitelist* that imports
 ``openai``, ``anthropic``, or ``llm_orchestrator`` (the public
 distribution name of ``libs/llm-orchestrator``). Both ``import``
 and ``from`` forms are recognised.
 """

    findings: list[Finding] = []
    for path in iter_source_files(root):
        rel = _normalise(path.relative_to(root))
        if _is_under(rel, whitelist):
            continue

        tree = _parse(path)
        if tree is None:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = (alias.name or "").split(".", 1)[0]
                    if root_mod in LLM_MODULES:
                        findings.append(
                            Finding(
                                path=rel,
                                lineno=node.lineno,
                                category="llm",
                                symbol=root_mod,
                                detail=(
                                    f"LLM library {root_mod!r} imported "
                                    "outside assistant-service / "
                                    "agent-runner-worker. the operational rule."
                                ),
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    root_mod = node.module.split(".", 1)[0]
                    if root_mod in LLM_MODULES:
                        findings.append(
                            Finding(
                                path=rel,
                                lineno=node.lineno,
                                category="llm",
                                symbol=root_mod,
                                detail=(
                                    f"LLM library {root_mod!r} imported "
                                    "outside assistant-service / "
                                    "agent-runner-worker. the operational rule."
                                ),
                            )
                        )
    return findings


# ---------------------------------------------------------------------------
# Scan: ``client.start_workflow`` inside activities
# ---------------------------------------------------------------------------


# Glob roots that count as activity packages. The exact convention is
# ``workers/<worker>/src/activities/`` based on the existing repository
# layout. Both ``workers/<worker>/activities/`` and
# ``workers/<worker>/src/activities/`` are accepted to keep the scanner
# robust against future packaging changes.
ACTIVITY_PATH_FRAGMENTS: tuple[str, ...] = (
    "/activities/",
)

# Method names whose presence as the trailing ``.attr`` of a call target
# is treated as a workflow-start call. ``start_workflow`` is the
# canonical Temporal client method; ``execute_workflow`` is the wait-
# for-result variant. ``start_child_workflow`` belongs in workflow code,
# not activity code.
WORKFLOW_START_METHODS: frozenset[str] = frozenset(
    {
        "start_workflow",
        "execute_workflow",
        "start_child_workflow",
    }
)


def scan_activities_start_workflow(
    root: Path = PLATFORM_ROOT,
) -> list[Finding]:
    """Find ``client.start_workflow``-shaped calls inside activity files.



 For every ``.py`` file whose workspace-relative path contains
 ``/activities/``, the scanner walks the AST and reports any:class:`ast.Call` whose target is a method named ``start_workflow``,
 ``execute_workflow``, or ``start_child_workflow`` (regardless of
 receiver — ``client.start_workflow``, ``self.client.start_workflow``,
 ``temporal_client.start_workflow``, etc.). The ``Await`` wrapper
 around the call is not relevant — the scan inspects the call node
 directly.

 Workflow-decision mantığı yalnız workflow modüllerinde olmalıdır;
 activity dosyalarında bu çağrıların bulunmaması'in
 statik karşılığıdır.
 """

    findings: list[Finding] = []
    for path in iter_source_files(root):
        rel = _normalise(path.relative_to(root))
        if not any(fragment in "/" + rel for fragment in ACTIVITY_PATH_FRAGMENTS):
            continue

        tree = _parse(path)
        if tree is None:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            method = _attribute_tail(node.func)
            if method is None or method not in WORKFLOW_START_METHODS:
                continue
            dotted = _dotted_name(node.func) or method
            findings.append(
                Finding(
                    path=rel,
                    lineno=node.lineno,
                    category="activity_start_workflow",
                    symbol=dotted,
                    detail=(
                        "Workflow-start call inside activity module. "
                        "the operational rule: workflow decision logic must "
                        "live in workers/*/workflows/, not activities/."
                    ),
                )
            )
    return findings


def _attribute_tail(node: ast.expr) -> str | None:
    """Return the trailing ``.attr`` of *node* if it is an ``Attribute``,
 else None.

 Examples
 --------
 * ``client.start_workflow`` → ``"start_workflow"``
 * ``self.tx.client.start_workflow`` → ``"start_workflow"``
 * ``foo`` → None (Call, not Attribute)
 * ``open`` → None (Name, not Attribute)
 """

    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ---------------------------------------------------------------------------
# Convenience aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanReport:
    """Aggregated results from running every scanner once.

 Each list contains:class:`Finding` instances filtered by the
 scanner's whitelist. The aggregator is intentionally lightweight —
 callers in invariant typically inspect the list lengths and
 surface the findings via ``assert not findings, format_findings(findings)``.
 """

    atlassian_http: list[Finding] = field(default_factory=list)
    ssh_docker: list[Finding] = field(default_factory=list)
    llm: list[Finding] = field(default_factory=list)
    activity_start_workflow: list[Finding] = field(default_factory=list)

    @property
    def all_findings(self) -> list[Finding]:
        """Concatenated view across every category, in scanner order."""

        return [
            *self.atlassian_http,
            *self.ssh_docker,
            *self.llm,
            *self.activity_start_workflow,
        ]


def run_full_scan(root: Path = PLATFORM_ROOT) -> ScanReport:
    """Run every scanner against *root* and return a:class:`ScanReport`.

 Convenience wrapper used by invariant-1.5)
 aggregate tests. The individual scanners remain available for
 fine-grained tests that wish to assert one the operational rule at a time.
 """

    return ScanReport(
        atlassian_http=scan_atlassian_http_calls(root),
        ssh_docker=scan_ssh_docker_calls(root),
        llm=scan_llm_calls(root),
        activity_start_workflow=scan_activities_start_workflow(root),
    )


def format_findings(findings: Sequence[Finding]) -> str:
    """Render *findings* as a multi-line bulleted string for assert messages.

 Each line follows the format
 ``<path>:<lineno> [<category>] <symbol> — <detail>``
 so a failing invariant surfaces every offending file at once.
 """

    if not findings:
        return ""
    lines = [
        f" - {f.path}:{f.lineno} [{f.category}] {f.symbol} — {f.detail}"
        for f in findings
    ]
    return "\n".join(lines)


__all__ = [
    "PLATFORM_ROOT",
    "SCAN_EXCLUDED_DIRS",
    "Finding",
    "ScanReport",
    "ATLASSIAN_HTTP_WHITELIST",
    "ATLASSIAN_HTTP_MODULES",
    "ATLASSIAN_HOST_PATTERNS",
    "SSH_DOCKER_WHITELIST",
    "SSH_MODULES",
    "DOCKER_MODULES",
    "LLM_WHITELIST",
    "LLM_MODULES",
    "ACTIVITY_PATH_FRAGMENTS",
    "WORKFLOW_START_METHODS",
    "iter_source_files",
    "scan_atlassian_http_calls",
    "scan_ssh_docker_calls",
    "scan_llm_calls",
    "scan_activities_start_workflow",
    "run_full_scan",
    "format_findings",
]
