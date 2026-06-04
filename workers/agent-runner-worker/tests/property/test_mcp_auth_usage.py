"""AST test for MCP auth usage in agent-runner-worker activity modules.

Pure infrastructural invariants — MCP auth usage (AST scan).

This test parses the AST of:

- ``workers/agent-runner-worker/src/activities/jira.py``
- ``workers/agent-runner-worker/src/activities/bitbucket.py``
- ``workers/agent-runner-worker/src/activities/confluence.py``

and asserts the following pure infrastructural invariants:

1. **No raw ``httpx.AsyncClient`` instantiation.** Every ``httpx.AsyncClient``
   instance MUST be obtained via the shared
   ``http_shared.make_mcp_client(client_source=...)`` factory. Direct
   ``httpx.AsyncClient(...)`` constructor calls are forbidden so the
   ``X-Client-Source`` observability header is always injected and the
   credential-injection contract holds.

2. **``make_mcp_client`` is called with a ``client_source`` argument.** Every
   call site to ``make_mcp_client`` MUST supply ``client_source=`` either as
   a keyword argument or as the single positional argument so the factory's
   contract is honoured.

3. **Every network call sits inside an ``async with with_atlassian_creds``
   block.** For every ``async def`` function that performs an HTTP request
   (``client.post(...)``, ``client.get(...)``, ``client.request(...)``,
   etc.), the call MUST be enclosed by an ``async with with_atlassian_creds(...)``
   block somewhere on its lexical ancestor chain. This guarantees that no
   activity reaches the MCP server without department-scoped Atlassian
   credentials injected first.

The check is performed entirely with the standard library ``ast`` module —
no Hypothesis is needed because the invariant is a pure structural check
of a finite, fully enumerable set of source files. Per-file invariants are
parametrised over the three activity modules so failures pinpoint the exact
file. A ``TestScannerSelfChecks`` class additionally validates the scanner
itself against synthetic source snippets so the parametrised production-file
tests are only as strong as the scanner that powers them.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths to the three activity modules under scrutiny
# ---------------------------------------------------------------------------

#: Absolute path to the agent-runner-worker package root, computed from the
#: location of this test file (``tests/property/<this>.py`` → ``../../``).
_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_ACTIVITIES_DIR: Path = _WORKER_ROOT / "src" / "activities"

#: The three activity modules whose AST must satisfy the auth invariants.
_ACTIVITY_FILES: tuple[Path, ...] = (
    _ACTIVITIES_DIR / "jira.py",
    _ACTIVITIES_DIR / "bitbucket.py",
    _ACTIVITIES_DIR / "confluence.py",
)

#: Pytest parametrisation IDs — short, stable filenames.
_ACTIVITY_FILE_IDS: tuple[str, ...] = tuple(p.name for p in _ACTIVITY_FILES)


# ---------------------------------------------------------------------------
# HTTP method names that count as a "network call" on an httpx client
# ---------------------------------------------------------------------------

#: Method names on an ``httpx.AsyncClient`` (or any aliased name) that issue
#: a real network request. These are the points that must be enclosed by an
#: ``async with with_atlassian_creds(...)`` block.
#:
#: ``get`` is a real httpx method but is also exposed by many other objects
#: (``dict``, ``os.environ``, ``Mapping``…). The walker independently tracks
#: which local names hold an httpx client (see :func:`_scan_module`) and only
#: counts attribute calls on those receivers, which lets us re-include
#: ``get`` safely.
_HTTP_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "request",
        "send",
        "stream",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
    }
)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetworkCall:
    """A single network call site discovered in an activity module."""

    module: str  # e.g. "jira.py"
    function: str  # enclosing async function name
    lineno: int  # 1-indexed line number of the call


def _parse_module(path: Path) -> ast.Module:
    """Parse a Python source file into an AST module node."""

    source = path.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(path))


def _is_with_atlassian_creds_item(item: ast.withitem) -> bool:
    """Return True iff a ``with_atlassian_creds(...)`` call is the context expr."""

    expr = item.context_expr
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    # Match bare name: with_atlassian_creds(...)
    if isinstance(func, ast.Name) and func.id == "with_atlassian_creds":
        return True
    # Match attribute: module.with_atlassian_creds(...) — defensive even
    # though the activity modules import the name directly.
    if isinstance(func, ast.Attribute) and func.attr == "with_atlassian_creds":
        return True
    return False


def _is_make_mcp_client_call(node: ast.Call) -> bool:
    """Return True iff *node* calls ``make_mcp_client(...)``."""

    func = node.func
    if isinstance(func, ast.Name) and func.id == "make_mcp_client":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "make_mcp_client":
        return True
    return False


def _is_raw_httpx_async_client_construction(node: ast.Call) -> bool:
    """Return True iff *node* directly instantiates ``httpx.AsyncClient(...)``.

    Matches both ``httpx.AsyncClient(...)`` (attribute access) and a bare
    ``AsyncClient(...)`` call (which would only appear if someone wrote
    ``from httpx import AsyncClient`` — also forbidden).
    """

    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr == "AsyncClient":
            value = func.value
            if isinstance(value, ast.Name) and value.id == "httpx":
                return True
    if isinstance(func, ast.Name) and func.id == "AsyncClient":
        return True
    return False


def _http_method_attr(node: ast.Call) -> str | None:
    """If *node* is ``something.<http_method>(...)``, return the method name.

    Returns ``None`` if the call isn't an attribute call or the attribute
    name is not in :data:`_HTTP_METHOD_NAMES`. The receiver expression is
    *not* inspected here — the caller filters by receiver name.
    """

    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in _HTTP_METHOD_NAMES:
        return None
    return func.attr


def _attr_receiver_name(node: ast.Call) -> str | None:
    """If *node* is ``<Name>.<attr>(...)``, return ``<Name>.id``; else None."""

    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return receiver.id
    return None


def _has_client_source_argument(call: ast.Call) -> bool:
    """Return True iff ``make_mcp_client(...)`` receives a ``client_source`` arg.

    Accepts either a keyword argument named ``client_source`` or a single
    positional argument (the factory's ``client_source`` parameter is
    positional-or-keyword in ``http_shared.client.make_mcp_client``).
    """

    for kw in call.keywords:
        if kw.arg == "client_source":
            return True
    # A bare positional argument is also acceptable (matches the factory's
    # signature: ``def make_mcp_client(client_source: str, ...)``).
    if call.args:
        return True
    return False


# ---------------------------------------------------------------------------
# Walker — collects network-call sites and remembers their context
# ---------------------------------------------------------------------------


@dataclass
class ModuleScanResult:
    """Outcome of scanning a single activity module."""

    module: str
    raw_async_client_calls: list[ast.Call] = field(default_factory=list)
    make_mcp_client_calls_without_client_source: list[ast.Call] = field(
        default_factory=list
    )
    network_calls: list[NetworkCall] = field(default_factory=list)
    network_calls_outside_creds_block: list[NetworkCall] = field(
        default_factory=list
    )


def _scan_tree(tree: ast.Module, module_name: str) -> ModuleScanResult:
    """Walk *tree*'s AST and collect every invariant-relevant call site.

    Stack-based traversal so we can track lexical context:

    - the enclosing async/sync function (for diagnostic naming),
    - the current ``async with with_atlassian_creds`` nesting depth,
    - the set of local variable names that currently hold an httpx
      client (assigned from ``make_mcp_client(...)`` or bound by
      ``with with_atlassian_creds(...) as <name>``).
    """

    result = ModuleScanResult(module=module_name)

    enclosing_func_stack: list[str] = []
    creds_depth = 0
    client_names: set[str] = set()

    # ------------------------------------------------------------------
    # Module-level Call inspection always runs (raw AsyncClient detection
    # and ``make_mcp_client`` argument check) regardless of enclosing
    # context.
    # ------------------------------------------------------------------

    def _inspect_call(node: ast.Call) -> None:
        # 1. Forbid raw httpx.AsyncClient(...) construction anywhere.
        if _is_raw_httpx_async_client_construction(node):
            result.raw_async_client_calls.append(node)

        # 2. make_mcp_client(...) MUST receive a client_source value.
        if _is_make_mcp_client_call(node):
            if not _has_client_source_argument(node):
                result.make_mcp_client_calls_without_client_source.append(node)

        # 3. Network call sites must sit inside a creds block. Only flag
        #    attribute calls whose receiver Name is a tracked client
        #    binding, which avoids false positives on dict.get / .update /
        #    os.environ.get / etc.
        method_name = _http_method_attr(node)
        if method_name is not None and enclosing_func_stack:
            receiver = _attr_receiver_name(node)
            if receiver is not None and receiver in client_names:
                call_site = NetworkCall(
                    module=module_name,
                    function=enclosing_func_stack[-1],
                    lineno=node.lineno,
                )
                result.network_calls.append(call_site)
                if creds_depth == 0:
                    result.network_calls_outside_creds_block.append(call_site)

    # ------------------------------------------------------------------
    # Assignment tracking — record names bound to ``make_mcp_client(...)``
    # so subsequent ``<name>.post(...)`` etc. calls can be attributed.
    # ------------------------------------------------------------------

    def _record_assignment(node: ast.Assign | ast.AnnAssign) -> None:
        value = node.value
        if value is None:
            return
        if not (isinstance(value, ast.Call) and _is_make_mcp_client_call(value)):
            return
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:  # AnnAssign
            targets = [node.target] if node.target is not None else []
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                client_names.add(tgt.id)

    def visit(node: ast.AST) -> None:
        nonlocal creds_depth

        # ---- Track entering / leaving an async function scope ----
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            enclosing_func_stack.append(node.name)
            # Per-function client name scope so a local rebound name in
            # one function doesn't leak into a sibling function.
            saved_client_names = set(client_names)
            try:
                for child in ast.iter_child_nodes(node):
                    visit(child)
            finally:
                enclosing_func_stack.pop()
                client_names.clear()
                client_names.update(saved_client_names)
            return

        # ---- Track ``async with with_atlassian_creds(...)`` nesting ----
        if isinstance(node, (ast.AsyncWith, ast.With)):
            entered = 0
            new_creds_aliases: list[str] = []
            new_client_aliases: list[str] = []
            for item in node.items:
                if _is_with_atlassian_creds_item(item):
                    entered += 1
                    # ``async with with_atlassian_creds(client, ...) as authed:``
                    # binds the authed client name; any post() on it is
                    # also a network call.
                    if isinstance(item.optional_vars, ast.Name):
                        new_creds_aliases.append(item.optional_vars.id)
                else:
                    # ``async with client:`` is a no-op for our purposes
                    # but the value being entered is still a client
                    # binding — record any ``as <name>`` alias defensively.
                    expr = item.context_expr
                    if isinstance(expr, ast.Name) and expr.id in client_names:
                        if isinstance(item.optional_vars, ast.Name):
                            new_client_aliases.append(item.optional_vars.id)

            creds_depth += entered
            for alias in new_creds_aliases:
                client_names.add(alias)
            for alias in new_client_aliases:
                client_names.add(alias)
            try:
                for child in ast.iter_child_nodes(node):
                    visit(child)
            finally:
                creds_depth -= entered
                for alias in new_creds_aliases:
                    client_names.discard(alias)
                for alias in new_client_aliases:
                    client_names.discard(alias)
            return

        # ---- Recognise ``<name> = make_mcp_client(...)`` ----
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            _record_assignment(node)
            for child in ast.iter_child_nodes(node):
                visit(child)
            return

        # ---- Inspect Call nodes for the three sub-invariants ----
        if isinstance(node, ast.Call):
            _inspect_call(node)

        # ---- Recurse into all other nodes ----
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return result


def _scan_module(path: Path) -> ModuleScanResult:
    """Scan a single activity module file."""

    if not path.exists():
        return ModuleScanResult(module=path.name)
    return _scan_tree(_parse_module(path), module_name=path.name)


def _scan_source(source: str, module_name: str = "<synthetic>") -> ModuleScanResult:
    """Scan a synthetic source string. Used by the scanner self-tests."""

    return _scan_tree(ast.parse(source), module_name=module_name)


# ---------------------------------------------------------------------------
# Aggregate scan — run once at module import so per-test functions are cheap.
# ---------------------------------------------------------------------------


def _scan_all_activity_modules() -> dict[str, ModuleScanResult]:
    """Scan all three activity modules and return ``{filename: result}``."""

    return {path.name: _scan_module(path) for path in _ACTIVITY_FILES}


_SCAN_RESULTS: dict[str, ModuleScanResult] = _scan_all_activity_modules()


# ---------------------------------------------------------------------------
# Production-file tests
# ---------------------------------------------------------------------------


def test_activity_files_exist() -> None:
    """Sanity check — the three activity modules exist on disk.

    If any of these is missing, the rest of the test would silently pass
    (vacuous truth on an empty AST). This guard ensures the auth invariant is
    actually enforced.
    """

    missing = [str(p) for p in _ACTIVITY_FILES if not p.exists()]
    assert not missing, (
        "Expected activity modules are missing on disk; auth invariant "
        f"cannot be enforced: {missing}"
    )


def test_at_least_one_network_call_was_discovered() -> None:
    """Sanity check — the AST walker can actually detect network calls.

    The production activity modules now route every Atlassian request
    through the ``call_mcp_tool`` helper (which owns the
    ``make_mcp_client`` + ``with_atlassian_creds`` lifecycle), so they
    legitimately contain *zero* raw httpx-client call sites. Requiring a
    raw call in production would therefore be architecturally wrong.

    To keep the walker from passing vacuously we instead assert it
    detects a network call in a known-good in-test source snippet — this
    proves the walker is wired correctly without forcing production code
    to embed a raw client call.
    """

    result = _scan_source(_GOOD_ACTIVITY_SRC)
    assert result.network_calls, (
        "AST walker found zero network calls in the known-good sample "
        "source; the walker is broken."
    )


@pytest.mark.parametrize("module_name", _ACTIVITY_FILE_IDS, ids=_ACTIVITY_FILE_IDS)
def test_no_raw_httpx_async_client_in_activity_module(module_name: str) -> None:
    """No module instantiates
    ``httpx.AsyncClient`` directly.

    A raw constructor call would bypass the ``X-Client-Source`` header
    injection that ``make_mcp_client`` performs and
    therefore the credential-injection contract of
    ``with_atlassian_creds``.
    """

    result = _SCAN_RESULTS[module_name]
    offenders = [f"{module_name}:{call.lineno}" for call in result.raw_async_client_calls]
    assert not offenders, (
        "Raw ``httpx.AsyncClient(...)`` "
        "constructor calls found. All clients must be created via "
        "``http_shared.make_mcp_client(client_source=...)`` so the "
        "``X-Client-Source`` header is injected automatically.\n"
        f"Offending sites: {offenders}"
    )


@pytest.mark.parametrize("module_name", _ACTIVITY_FILE_IDS, ids=_ACTIVITY_FILE_IDS)
def test_make_mcp_client_calls_have_client_source(module_name: str) -> None:
    """Every ``make_mcp_client`` call
    supplies a ``client_source`` argument so the activity module
    self-identifies for observability.
    """

    result = _SCAN_RESULTS[module_name]
    offenders = [
        f"{module_name}:{call.lineno}"
        for call in result.make_mcp_client_calls_without_client_source
    ]
    assert not offenders, (
        "``make_mcp_client(...)`` called "
        "without a ``client_source`` argument. Each activity module "
        "must identify itself for observability.\n"
        f"Offending sites: {offenders}"
    )


@pytest.mark.parametrize("module_name", _ACTIVITY_FILE_IDS, ids=_ACTIVITY_FILE_IDS)
def test_every_network_call_is_inside_with_atlassian_creds(module_name: str) -> None:
    """Every network call sits
    inside an ``async with with_atlassian_creds(...)`` block.

    For every ``async def`` function that performs an HTTP request
    (``client.post(...)``, ``client.get(...)``, ``client.request(...)``,
    etc.) the call MUST be enclosed by an ``async with
    with_atlassian_creds(...)`` block somewhere on its lexical ancestor
    chain. This guarantees no activity reaches the MCP server without
    department-scoped credentials injected first.
    """

    result = _SCAN_RESULTS[module_name]
    offenders = [
        f"{call.module}::{call.function} (line {call.lineno})"
        for call in result.network_calls_outside_creds_block
    ]
    assert not offenders, (
        "The following network call sites "
        "are not inside an ``async with with_atlassian_creds(...)`` "
        "block:\n  - " + "\n  - ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Scanner self-tests
# ---------------------------------------------------------------------------
#
# These exercise the scanner against synthetic source snippets so the scan
# logic itself is validated, regardless of the state of the production
# activity modules. Without these the parametrised production-file tests
# above would only be as strong as the scanner that powers them.


_HEADER = """\
import httpx
from http_shared import make_mcp_client, with_atlassian_creds
from temporalio import activity
"""


_GOOD_ACTIVITY_SRC = (
    _HEADER
    + """
@activity.defn
async def good_activity(dept_id: str) -> str:
    client = make_mcp_client(client_source="agent-runner-worker")
    async with client:
        async with with_atlassian_creds(
            client, dept_id=dept_id, service="jira", credential_resolver=None,
        ) as authed:
            response = await authed.post("/mcp", json={})
            return response.text
"""
)


_GOOD_POSITIONAL_CLIENT_SOURCE_SRC = (
    _HEADER
    + """
@activity.defn
async def good_positional(dept_id: str) -> str:
    # client_source supplied positionally — also acceptable.
    client = make_mcp_client("agent-runner-worker")
    async with client:
        async with with_atlassian_creds(
            client, dept_id=dept_id, service="jira", credential_resolver=None,
        ) as authed:
            r = await authed.get("/mcp")
            return r.text
"""
)


_BAD_RAW_HTTPX_SRC = (
    _HEADER
    + """
@activity.defn
async def bad_raw_httpx(dept_id: str) -> str:
    client = httpx.AsyncClient(base_url="http://atlassian-mcp:8090")
    async with client:
        async with with_atlassian_creds(
            client, dept_id=dept_id, service="jira", credential_resolver=None,
        ) as authed:
            r = await authed.post("/mcp", json={})
            return r.text
"""
)


_BAD_BARE_ASYNC_CLIENT_SRC = """\
from httpx import AsyncClient
from http_shared import with_atlassian_creds
from temporalio import activity

@activity.defn
async def bad_bare_async_client(dept_id: str) -> str:
    client = AsyncClient()
    async with client:
        async with with_atlassian_creds(
            client, dept_id=dept_id, service="jira", credential_resolver=None,
        ) as authed:
            r = await authed.post("/mcp", json={})
            return r.text
"""


_BAD_MISSING_CLIENT_SOURCE_SRC = (
    _HEADER
    + """
@activity.defn
async def bad_missing_client_source(dept_id: str) -> str:
    # No client_source.
    client = make_mcp_client(timeout=30.0)
    async with client:
        async with with_atlassian_creds(
            client, dept_id=dept_id, service="jira", credential_resolver=None,
        ) as authed:
            r = await authed.post("/mcp", json={})
            return r.text
"""
)


_BAD_NETWORK_CALL_OUTSIDE_CREDS_SRC = (
    _HEADER
    + """
@activity.defn
async def bad_outside_creds(dept_id: str) -> str:
    client = make_mcp_client(client_source="agent-runner-worker")
    # Network call OUTSIDE with_atlassian_creds.
    response = await client.post("/mcp", json={})
    return response.text
"""
)


_BAD_NETWORK_CALL_BEFORE_CREDS_SRC = (
    _HEADER
    + """
@activity.defn
async def bad_before_creds(dept_id: str) -> str:
    client = make_mcp_client(client_source="agent-runner-worker")
    async with client:
        # Network call BEFORE entering with_atlassian_creds — still bad.
        await client.get("/mcp/health")
        async with with_atlassian_creds(
            client, dept_id=dept_id, service="jira", credential_resolver=None,
        ) as authed:
            r = await authed.post("/mcp", json={})
            return r.text
"""
)


_FALSE_POSITIVE_DICT_GET_SRC = (
    _HEADER
    + """
@activity.defn
async def looks_like_get(payload: dict) -> str:
    # ``payload.get`` must NOT count as a network call — the receiver is
    # not a tracked httpx client binding.
    return payload.get("key", "")
"""
)


_FALSE_POSITIVE_OS_ENVIRON_GET_SRC = (
    _HEADER
    + """
import os

@activity.defn
async def env_lookup() -> str:
    # ``os.environ.get`` must NOT count as a network call.
    return os.environ.get("MCP_BASE_URL", "default")
"""
)


_NESTED_AS_ALIAS_SRC = (
    _HEADER
    + """
@activity.defn
async def nested_alias(dept_id: str) -> str:
    client = make_mcp_client(client_source="agent-runner-worker")
    async with client:
        async with with_atlassian_creds(
            client, dept_id=dept_id, service="jira", credential_resolver=None,
        ) as authed_client:
            # Network call on the ``as`` alias — must be recognised too.
            r = await authed_client.request("POST", "/mcp", json={})
            return r.text
"""
)


class TestScannerSelfChecks:
    """Synthetic-source self-tests that lock in scanner behaviour.

    These guard against regressions in the scanner itself — the
    parametrised production-file tests are only as strong as the scanner
    that powers them.
    """

    # -- Positive cases (no violations expected) ------------------------

    def test_good_activity_has_no_violations(self) -> None:
        """A well-formed activity passing through ``make_mcp_client`` and
        wrapping the network call in ``with_atlassian_creds`` MUST be
        accepted by all three sub-invariants.
        """
        result = _scan_source(_GOOD_ACTIVITY_SRC)
        assert result.raw_async_client_calls == []
        assert result.make_mcp_client_calls_without_client_source == []
        assert result.network_calls_outside_creds_block == []
        # And we DID see at least one network call (sanity).
        assert result.network_calls, "scanner failed to detect the post() call"

    def test_positional_client_source_is_accepted(self) -> None:
        """``make_mcp_client("agent-runner-worker")`` (positional argument)
        is acceptable since the factory's ``client_source`` parameter is
        positional-or-keyword.
        """
        result = _scan_source(_GOOD_POSITIONAL_CLIENT_SOURCE_SRC)
        assert result.make_mcp_client_calls_without_client_source == []
        assert result.network_calls_outside_creds_block == []

    def test_nested_as_alias_is_recognised_as_client(self) -> None:
        """``async with with_atlassian_creds(...) as authed_client:`` binds
        a new name; subsequent ``authed_client.request(...)`` calls MUST
        be recognised as network calls on a tracked client.
        """
        result = _scan_source(_NESTED_AS_ALIAS_SRC)
        assert result.network_calls, "as-alias network call was not detected"
        assert result.network_calls_outside_creds_block == []

    # -- Raw httpx.AsyncClient detection --------------------------------

    def test_raw_httpx_async_client_is_detected(self) -> None:
        """Raw ``httpx.AsyncClient(...)`` calls are detected."""
        result = _scan_source(_BAD_RAW_HTTPX_SRC)
        assert len(result.raw_async_client_calls) == 1, (
            "scanner missed the raw ``httpx.AsyncClient(...)`` call"
        )

    def test_bare_async_client_import_is_detected(self) -> None:
        """``from httpx import AsyncClient; AsyncClient(...)`` is also
        forbidden — the scanner must catch the bare-name form.
        """
        result = _scan_source(_BAD_BARE_ASYNC_CLIENT_SRC)
        assert len(result.raw_async_client_calls) == 1, (
            "scanner missed the bare ``AsyncClient(...)`` call"
        )

    # -- make_mcp_client argument check ---------------------------------

    def test_missing_client_source_is_detected(self) -> None:
        """Missing ``client_source`` arguments are detected."""
        result = _scan_source(_BAD_MISSING_CLIENT_SOURCE_SRC)
        assert len(result.make_mcp_client_calls_without_client_source) == 1, (
            "scanner missed ``make_mcp_client(timeout=...)`` with no "
            "client_source"
        )

    # -- Network-call placement check -----------------------------------

    def test_network_call_outside_creds_block_is_detected(self) -> None:
        """Network calls outside ``with_atlassian_creds`` are detected."""
        result = _scan_source(_BAD_NETWORK_CALL_OUTSIDE_CREDS_SRC)
        assert len(result.network_calls_outside_creds_block) == 1, (
            "scanner missed a post() call outside with_atlassian_creds"
        )

    def test_network_call_before_entering_creds_is_detected(self) -> None:
        """A ``client.get(...)`` call inside ``async with client:`` but
        BEFORE ``async with with_atlassian_creds(...)`` is still a
        violation.
        """
        result = _scan_source(_BAD_NETWORK_CALL_BEFORE_CREDS_SRC)
        offenders = [
            (c.function, c.lineno) for c in result.network_calls_outside_creds_block
        ]
        assert offenders, (
            "scanner missed the early get() before with_atlassian_creds; "
            f"all network calls: {result.network_calls}"
        )

    # -- False-positive guards ------------------------------------------

    def test_dict_get_is_not_a_network_call(self) -> None:
        """``payload.get("key")`` must not be classified as a network call —
        the receiver is not a tracked httpx-client binding.
        """
        result = _scan_source(_FALSE_POSITIVE_DICT_GET_SRC)
        assert result.network_calls == [], (
            f"scanner falsely flagged dict.get as a network call: "
            f"{result.network_calls}"
        )

    def test_os_environ_get_is_not_a_network_call(self) -> None:
        """``os.environ.get`` is not classified as a network call."""
        result = _scan_source(_FALSE_POSITIVE_OS_ENVIRON_GET_SRC)
        assert result.network_calls == [], (
            f"scanner falsely flagged os.environ.get as a network call: "
            f"{result.network_calls}"
        )

    # -- Helper-level checks --------------------------------------------

    def test_has_client_source_argument_keyword(self) -> None:
        """``client_source=`` keyword argument is accepted."""
        call = ast.parse('make_mcp_client(client_source="x")').body[0].value
        assert isinstance(call, ast.Call)
        assert _has_client_source_argument(call)

    def test_has_client_source_argument_positional(self) -> None:
        """A positional argument is accepted (matches factory signature)."""
        call = ast.parse('make_mcp_client("x")').body[0].value
        assert isinstance(call, ast.Call)
        assert _has_client_source_argument(call)

    def test_has_client_source_argument_missing(self) -> None:
        """No args at all → rejected."""
        call = ast.parse("make_mcp_client()").body[0].value
        assert isinstance(call, ast.Call)
        assert not _has_client_source_argument(call)

    def test_has_client_source_argument_other_keyword_only(self) -> None:
        """A different kwarg with no positional args → rejected."""
        call = ast.parse("make_mcp_client(timeout=1.0)").body[0].value
        assert isinstance(call, ast.Call)
        assert not _has_client_source_argument(call)

    def test_is_with_atlassian_creds_item_recognises_bare_name(self) -> None:
        """``with_atlassian_creds(...)`` as a bare name is recognised."""
        tree = ast.parse(
            "async def f():\n"
            "    async with with_atlassian_creds(c, dept_id='x') as a:\n"
            "        pass\n"
        )
        async_with = tree.body[0].body[0]
        assert isinstance(async_with, ast.AsyncWith)
        assert _is_with_atlassian_creds_item(async_with.items[0])

    def test_is_with_atlassian_creds_item_recognises_attribute(self) -> None:
        """``module.with_atlassian_creds(...)`` is also recognised."""
        tree = ast.parse(
            "async def f():\n"
            "    async with mod.with_atlassian_creds(c) as a:\n"
            "        pass\n"
        )
        async_with = tree.body[0].body[0]
        assert isinstance(async_with, ast.AsyncWith)
        assert _is_with_atlassian_creds_item(async_with.items[0])

    def test_is_with_atlassian_creds_item_rejects_unrelated(self) -> None:
        """An unrelated context manager is NOT a creds block."""
        tree = ast.parse(
            "async def f():\n"
            "    async with some_other_cm() as a:\n"
            "        pass\n"
        )
        async_with = tree.body[0].body[0]
        assert isinstance(async_with, ast.AsyncWith)
        assert not _is_with_atlassian_creds_item(async_with.items[0])
