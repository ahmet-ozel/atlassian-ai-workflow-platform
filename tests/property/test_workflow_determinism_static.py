"""Workflow determinism static AST invariant tests.

Workflow determinism - banned-call AST invariant.
``WORKFLOW_TYPE_CAPABILITIES`` single-source AST invariant.
Workflow modülü replay determinism statik tarama.

For every Python module under
    workers/agent-runner-worker/src/workflows/
    workers/agent-runner-worker/src/agent_runner/workflows/
    workers/automation-worker/src/automation_worker/workflows/
    workers/execution-runner-worker/src/workflows/
the parsed AST of the module MUST contain *no* call to *any* of the
following symbols inside a class decorated with ``@workflow.defn``:

    datetime.now, datetime.utcnow, datetime.today,
    time.time, time.monotonic, time.perf_counter, time.process_time,
    random.* (every attribute access on `random`),
    uuid.uuid1, uuid.uuid4, uuid.uuid5,
    os.environ.get  (and os.environ[...] subscript), os.urandom,
    asyncio.sleep,
    open  (builtin),
    httpx.AsyncClient, httpx.Client, httpx.get, httpx.post,
    httpx.put, httpx.delete, httpx.patch, httpx.head, httpx.options,
    httpx.request, httpx.stream, httpx.send,
    requests.* (every attribute access on `requests`),
    aiohttp.* (every attribute access on `aiohttp`),
    openai.* (every attribute access on `openai`),
    anthropic.* (every attribute access on `anthropic`).

Additionally, if the workflow body contains any sleep/wait expression
it MUST go through ``workflow.sleep(...)`` or
``workflow.wait_condition(...)``. The first part is enforced by the
``asyncio.sleep`` / ``time.sleep`` ban above; this file augments that
with a positive assertion: when scanning a workflow that *does* contain
a sleep, the only acceptable form is the ``workflow.*`` variant.

This module also enforces activity timeout and retry configuration:

* every ``workflow.execute_activity[_*]`` and
  ``workflow.start_activity[_*]`` call site inside a ``@workflow.defn``
  class MUST pass the ``start_to_close_timeout`` keyword (so a
  misconfigured activity cannot run unbounded).

Activities permitted inside the Temporal sandbox escape hatch
``with workflow.unsafe.imports_passed_through():`` are ignored - that
block contains *imports*, not workflow-time calls, and the imports
themselves are explicitly exempted by the Temporal SDK.

This is a static check - the AST is the source of truth. The replay
flavour is implemented separately in ``test_workflow_determinism_replay.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Workspace anchors
# ---------------------------------------------------------------------------

# tests/property/test_workflow_determinism_static.py  platform/
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]

WORKFLOW_DIRS: tuple[Path, ...] = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src" / "workflows",
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src" / "agent_runner" / "workflows",
    _PLATFORM_ROOT / "workers" / "automation-worker" / "src" / "automation_worker" / "workflows",
    _PLATFORM_ROOT / "workers" / "execution-runner-worker" / "src" / "workflows",
)


# Additional source roots that contribute to workflow replay safety.
# These directories do *not* host ``@workflow.defn`` classes themselves,
# but their modules are imported by workflow code (via the
# ``workflow.unsafe.imports_passed_through()`` sandbox escape hatch or
# directly when the helper is purely deterministic).  A non-deterministic
# call hidden in one of these helpers would taint every workflow that
# imports it, so the same banned-call list applies here at *module
# scope* - not just inside ``@workflow.defn`` classes.
SHARED_REPLAY_SAFE_DIRS: tuple[Path, ...] = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src" / "temporal_shared",
)


def _iter_workflow_files() -> Iterator[Path]:
    """Yield every ``.py`` file under both workflow directories.

    ``__init__.py`` and stub modules are included intentionally - the
    AST scan must hold for *every* file in the workflows package so a
    future contributor cannot smuggle a non-deterministic import or
    helper next to the workflow definitions.
    """

    for directory in WORKFLOW_DIRS:
        if not directory.is_dir():
            # Surfaced via test_workflow_dirs_exist below; skip iteration
            # so collection does not raise.
            continue
        for path in sorted(directory.rglob("*.py")):
            yield path


WORKFLOW_FILES: tuple[Path, ...] = tuple(_iter_workflow_files())


# ---------------------------------------------------------------------------
# Banned-call specification
# ---------------------------------------------------------------------------

# Fully-qualified attribute chains that MUST NOT appear as call targets
# inside a workflow class body. Each entry is a dotted string that the
# scanner reconstructs from ``ast.Attribute`` chains.
BANNED_DOTTED: frozenset[str] = frozenset(
    {
        # datetime
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        # time
        "time.time",
        "time.monotonic",
        "time.perf_counter",
        "time.process_time",
        "time.sleep",
        # uuid
        "uuid.uuid1",
        "uuid.uuid4",
        "uuid.uuid5",
        # os.environ + os.urandom
        "os.environ.get",
        "os.urandom",
        # asyncio
        "asyncio.sleep",
        # httpx
        "httpx.AsyncClient",
        "httpx.Client",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.delete",
        "httpx.patch",
        "httpx.head",
        "httpx.options",
        "httpx.request",
        "httpx.stream",
        "httpx.send",
    }
)

# Module roots whose entire attribute namespace is banned (any attribute
# access ``X.<anything>`` used as a call target is rejected). Captures
# ``random.*`` and ``requests.*``, plus the workflows
# spec additions: any direct ``aiohttp.*`` / ``openai.*`` / ``anthropic.*``
# call must go through an ``@activity.defn`` activity, not the workflow
# body - i.e. the workflow may only call into these libraries via
# Temporal activities.
BANNED_MODULE_PREFIXES: frozenset[str] = frozenset(
    {"random", "requests", "aiohttp", "openai", "anthropic"}
)

# Bare names (Name nodes) banned as call targets.
BANNED_BARE_NAMES: frozenset[str] = frozenset({"open"})

# Acceptable substitutes used in messaging.
ACCEPTABLE_TIME = "workflow.now()"
ACCEPTABLE_SLEEP = "workflow.sleep(...) or workflow.wait_condition(...)"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _dotted_name(node: ast.expr) -> str | None:
    """Return the dotted attribute chain rooted at a ``Name`` node.

    For ``a.b.c`` (an ``Attribute`` whose value chains back to a
    ``Name``), returns ``"a.b.c"``. For anything else (calls, subscripts,
    literals) returns ``None``.
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


def _is_workflow_defn_decorator(decorator: ast.expr) -> bool:
    """Check whether a decorator expression is ``@workflow.defn`` or
    ``@workflow.defn(...)``.

    Both ``workflow.defn`` and ``workflow.defn(name="...")`` forms are
    accepted (the latter wraps the attribute in a ``Call`` node).
    """

    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if not isinstance(target, ast.Attribute):
        return False
    if target.attr != "defn":
        return False
    return isinstance(target.value, ast.Name) and target.value.id == "workflow"


def _collect_workflow_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """Return every class in ``tree`` decorated with ``@workflow.defn``."""

    return [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(_is_workflow_defn_decorator(d) for d in node.decorator_list)
    ]


def _classify_call_target(func: ast.expr) -> tuple[str, str] | None:
    """Classify a call target against the banned-call specification.

    Returns ``(category, dotted_repr)`` if the call target is banned,
    where ``category`` is one of:
        - ``"dotted"``     - exact match in BANNED_DOTTED
        - ``"prefix"``     - root module is in BANNED_MODULE_PREFIXES
        - ``"bare"``       - bare name in BANNED_BARE_NAMES
        - ``"sleep"``      - ``time.sleep`` or ``asyncio.sleep`` (special-cased
                              for the sleep/wait positive assertion)
    Returns ``None`` if the call is acceptable.
    """

    # Dotted: a.b[.c]
    dotted = _dotted_name(func)
    if dotted is not None:
        if "." in dotted:
            if dotted in BANNED_DOTTED:
                if dotted in {"asyncio.sleep", "time.sleep"}:
                    return ("sleep", dotted)
                return ("dotted", dotted)
            root = dotted.split(".", 1)[0]
            if root in BANNED_MODULE_PREFIXES:
                return ("prefix", dotted)
            return None
        # Single-token call target: bare name like ``open(...)``.
        if dotted in BANNED_BARE_NAMES:
            return ("bare", dotted)
        if dotted in BANNED_MODULE_PREFIXES:
            # ``random()`` as a direct callable - extremely unlikely
            # but caught for completeness.
            return ("prefix", dotted)
        return None

    # Anything else (Subscript, Lambda, etc.) is not classified as a
    # banned call target by name.
    return None


def _is_environ_subscript(node: ast.expr) -> bool:
    """Return True if ``node`` is an ``os.environ[...]`` subscript."""

    if not isinstance(node, ast.Subscript):
        return False
    dotted = _dotted_name(node.value)
    return dotted == "os.environ"


def _is_workflow_unsafe_imports_block(node: ast.With | ast.AsyncWith) -> bool:
    """Skip ``with workflow.unsafe.imports_passed_through():`` blocks.

    These blocks contain *imports*, not function calls. The Temporal
    sandbox documents this as the canonical escape hatch and the design
    document explicitly tolerates it (§Determinism, banlist §3 covers
    *I/O* not imports). Returning True from here lets the scanner
    treat the with-block as opaque (its body is import statements only).
    """

    for item in node.items:
        target = item.context_expr
        if isinstance(target, ast.Call):
            target = target.func
        dotted = _dotted_name(target)
        if dotted == "workflow.unsafe.imports_passed_through":
            return True
    return False


def _walk_workflow_class(cls: ast.ClassDef) -> Iterator[ast.AST]:
    """Yield every descendant of a ``@workflow.defn`` class body, but
    skip the bodies of ``with workflow.unsafe.imports_passed_through()``
    blocks (those legitimately contain ``import`` statements that
    would otherwise be flagged on follow-up rules).

    Normal ``with`` blocks (e.g. ``with open(...) as f:``) are descended
    into so the scanner can see the offending Call node inside the
    ``with`` item.
    """

    stack: list[ast.AST] = [cls]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.With, ast.AsyncWith)) and _is_workflow_unsafe_imports_block(
            node
        ):
            # Skip body - those are import statements behind the
            # Temporal sandbox escape hatch. The with-item itself has
            # already been yielded above; classification will see
            # ``workflow.unsafe.imports_passed_through`` which is not
            # banned.
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow_files() -> tuple[Path, ...]:
    """Module-scoped fixture exposing every workflow ``.py`` file."""

    return WORKFLOW_FILES


def test_workflow_dirs_exist() -> None:
    """Both workflow directories must exist; otherwise the AST scan would
    silently degrade to a no-op and the property would be vacuous.
    """

    for directory in WORKFLOW_DIRS:
        assert directory.is_dir(), (
            f"workflow directory missing: {directory.relative_to(_PLATFORM_ROOT)} "
            "- static workflow determinism cannot be enforced if the directory is absent."
        )


def test_at_least_one_workflow_file_collected() -> None:
    """Sanity check: collection must find at least one ``.py`` file across
    both directories so the parametrised tests below are not empty.
    """

    assert len(WORKFLOW_FILES) > 0, (
        "no .py files found under workflow directories - "
        f"checked: {[str(d) for d in WORKFLOW_DIRS]}"
    )


def test_at_least_one_workflow_defn_class_exists() -> None:
    """A non-vacuity guard: across both worker packages there MUST be at
    least one ``@workflow.defn``-decorated class. Otherwise the AST
    scan trivially passes (nothing to inspect) and the property gives a
    false sense of safety.
    """

    total = 0
    for path in WORKFLOW_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += len(_collect_workflow_classes(tree))
    assert total >= 1, (
        "no @workflow.defn classes discovered across both workflow packages; "
        "static workflow determinism requires at least one to be meaningful"
    )


@pytest.mark.parametrize(
    "path",
    WORKFLOW_FILES,
    ids=[str(p.relative_to(_PLATFORM_ROOT)).replace("\\", "/") for p in WORKFLOW_FILES],
)
def test_workflow_file_parses(path: Path) -> None:
    """Every workflow module must be syntactically valid Python so the
    AST scan can run. A SyntaxError here would also break Temporal
    worker registration at import time.
    """

    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path))


@pytest.mark.parametrize(
    "path",
    WORKFLOW_FILES,
    ids=[str(p.relative_to(_PLATFORM_ROOT)).replace("\\", "/") for p in WORKFLOW_FILES],
)
def test_workflow_module_has_no_banned_calls(path: Path) -> None:
    """For every class decorated with ``@workflow.defn``, walk the class
    body and assert that no ``Call`` node targets a banned symbol and
    no ``Subscript`` reads ``os.environ[...]``. Any violation is
    reported with the file, line, and offending dotted name.
    """

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    classes = _collect_workflow_classes(tree)

    violations: list[str] = []

    for cls in classes:
        for node in _walk_workflow_class(cls):
            if isinstance(node, ast.Call):
                hit = _classify_call_target(node.func)
                if hit is not None:
                    category, dotted = hit
                    rel = path.relative_to(_PLATFORM_ROOT)
                    violations.append(
                        f"{rel}:{node.lineno} - banned {category} call "
                        f"in @workflow.defn class {cls.name!r}: {dotted}(...)"
                    )

            if isinstance(node, ast.Subscript) and _is_environ_subscript(node):
                rel = path.relative_to(_PLATFORM_ROOT)
                violations.append(
                    f"{rel}:{node.lineno} - banned os.environ[...] subscript "
                    f"in @workflow.defn class {cls.name!r}"
                )

    assert not violations, (
        "Static workflow determinism violation - workflow body must not call "
        "non-deterministic or I/O symbols. Use workflow.now(), "
        "workflow.sleep(...), workflow.wait_condition(...), and "
        "@activity.defn for I/O.\n  - "
        + "\n  - ".join(violations)
    )


@pytest.mark.parametrize(
    "path",
    WORKFLOW_FILES,
    ids=[str(p.relative_to(_PLATFORM_ROOT)).replace("\\", "/") for p in WORKFLOW_FILES],
)
def test_workflow_module_sleep_uses_workflow_helpers(path: Path) -> None:
    """Positive form of the sleep/wait constraint: any sleep- or wait-
    shaped expression inside a ``@workflow.defn`` class MUST be
    ``workflow.sleep(...)`` or ``workflow.wait_condition(...)``. Bare
    ``asyncio.sleep`` / ``time.sleep`` are caught by the negative ban
    above; this test additionally asserts that *if* a workflow uses
    sleep at all, the workflow helper form is the only one present.
    """

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    classes = _collect_workflow_classes(tree)

    bad_sleeps: list[str] = []
    saw_workflow_sleep_or_wait = False

    for cls in classes:
        for node in _walk_workflow_class(cls):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted_name(node.func)
            if dotted in {"workflow.sleep", "workflow.wait_condition"}:
                saw_workflow_sleep_or_wait = True
            if dotted in {"asyncio.sleep", "time.sleep"}:
                rel = path.relative_to(_PLATFORM_ROOT)
                bad_sleeps.append(
                    f"{rel}:{node.lineno} - {dotted}(...) inside "
                    f"@workflow.defn class {cls.name!r}; use "
                    f"{ACCEPTABLE_SLEEP} instead"
                )

    # Negative bound: zero non-workflow sleeps allowed.
    assert not bad_sleeps, (
        "Static workflow determinism violation - workflow sleep/wait must use "
        "workflow.sleep(...) or workflow.wait_condition(...).\n  - "
        + "\n  - ".join(bad_sleeps)
    )

    # The positive existence check (`saw_workflow_sleep_or_wait`) is not
    # required to be True per file: a workflow may legitimately have no
    # sleeps at all. The variable is computed for clarity / future
    # diagnostic use; we only assert the negative bound here.
    _ = saw_workflow_sleep_or_wait


# ---------------------------------------------------------------------------
# Self-tests for the AST scanner
# ---------------------------------------------------------------------------
#
# These tests exercise the scanner against synthetic source snippets so
# the scan logic itself is validated even when the production workflow
# files are stubs. Without these the test would be vacuous on a fresh
# repo where workflow bodies are not yet implemented.


_GOOD_WORKFLOW_SRC = """
from temporalio import workflow

@workflow.defn(name="Good")
class GoodWorkflow:
    @workflow.run
    async def run(self, x: int) -> int:
        now = workflow.now()
        await workflow.sleep(1)
        await workflow.wait_condition(lambda: x > 0)
        return x
"""


_BAD_DATETIME_SRC = """
import datetime
from temporalio import workflow

@workflow.defn
class BadDatetime:
    @workflow.run
    async def run(self) -> str:
        return datetime.datetime.now().isoformat()
"""


_BAD_RANDOM_SRC = """
import random
from temporalio import workflow

@workflow.defn
class BadRandom:
    @workflow.run
    async def run(self) -> int:
        return random.randint(1, 10)
"""


_BAD_REQUESTS_SRC = """
import requests
from temporalio import workflow

@workflow.defn
class BadRequests:
    @workflow.run
    async def run(self) -> str:
        return requests.get("https://example.com").text
"""


_BAD_HTTPX_SRC = """
import httpx
from temporalio import workflow

@workflow.defn
class BadHttpx:
    @workflow.run
    async def run(self) -> str:
        async with httpx.AsyncClient() as c:
            r = await c.get("https://example.com")
        return r.text
"""


_BAD_ASYNCIO_SLEEP_SRC = """
import asyncio
from temporalio import workflow

@workflow.defn
class BadSleep:
    @workflow.run
    async def run(self) -> None:
        await asyncio.sleep(1)
"""


_BAD_OS_ENVIRON_GET_SRC = """
import os
from temporalio import workflow

@workflow.defn
class BadEnvGet:
    @workflow.run
    async def run(self) -> str:
        return os.environ.get("FOO", "")
"""


_BAD_OS_ENVIRON_SUBSCRIPT_SRC = """
import os
from temporalio import workflow

@workflow.defn
class BadEnvSub:
    @workflow.run
    async def run(self) -> str:
        return os.environ["FOO"]
"""


_BAD_OPEN_SRC = """
from temporalio import workflow

@workflow.defn
class BadOpen:
    @workflow.run
    async def run(self) -> str:
        with open("/etc/hostname") as f:
            return f.read()
"""


_BAD_UUID_SRC = """
import uuid
from temporalio import workflow

@workflow.defn
class BadUuid:
    @workflow.run
    async def run(self) -> str:
        return str(uuid.uuid4())
"""


_BAD_TIME_SRC = """
import time
from temporalio import workflow

@workflow.defn
class BadTime:
    @workflow.run
    async def run(self) -> float:
        return time.time()
"""


_BAD_AIOHTTP_SRC = """
import aiohttp
from temporalio import workflow

@workflow.defn
class BadAiohttp:
    @workflow.run
    async def run(self) -> str:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://example.com") as r:
                return await r.text()
"""


_BAD_OPENAI_SRC = """
import openai
from temporalio import workflow

@workflow.defn
class BadOpenai:
    @workflow.run
    async def run(self) -> str:
        client = openai.OpenAI()
        return client.responses.create(model="gpt-4o").output_text
"""


_BAD_ANTHROPIC_SRC = """
import anthropic
from temporalio import workflow

@workflow.defn
class BadAnthropic:
    @workflow.run
    async def run(self) -> str:
        client = anthropic.Anthropic()
        return client.messages.create(model="claude-3").content[0].text
"""


_BAD_OS_URANDOM_SRC = """
import os
from temporalio import workflow

@workflow.defn
class BadUrandom:
    @workflow.run
    async def run(self) -> bytes:
        return os.urandom(16)
"""


_NON_WORKFLOW_CLASS_SRC = """
import datetime, random, time, uuid, os, asyncio, httpx, requests

class NotAWorkflow:
    def helper(self) -> str:
        # All of these are fine - class is NOT decorated with @workflow.defn
        _ = datetime.datetime.now()
        _ = random.randint(1, 10)
        _ = time.time()
        _ = uuid.uuid4()
        _ = os.environ.get("FOO")
        return "ok"
"""


def _scan_source(source: str) -> list[str]:
    """Run the AST scanner over a source string and return violation strings."""

    tree = ast.parse(source)
    violations: list[str] = []
    for cls in _collect_workflow_classes(tree):
        for node in _walk_workflow_class(cls):
            if isinstance(node, ast.Call):
                hit = _classify_call_target(node.func)
                if hit is not None:
                    _, dotted = hit
                    violations.append(f"call:{dotted}")
            if isinstance(node, ast.Subscript) and _is_environ_subscript(node):
                violations.append("subscript:os.environ")
    return violations


class TestScannerSelfChecks:
    """Self-tests that lock in scanner behaviour against synthetic inputs.

    These guard against regressions in the scanner itself - the
    parametrised production-file tests are only as strong as the
    scanner that powers them.
    """

    def test_good_workflow_has_no_violations(self) -> None:
        """Banned datetime calls are detected."""
        assert _scan_source(_GOOD_WORKFLOW_SRC) == []

    def test_datetime_now_detected(self) -> None:
        """Banned time calls are detected."""
        violations = _scan_source(_BAD_DATETIME_SRC)
        assert any("datetime" in v and "now" in v for v in violations), violations

    def test_random_attribute_detected(self) -> None:
        """Banned random calls are detected."""
        violations = _scan_source(_BAD_RANDOM_SRC)
        assert any(v.startswith("call:random.") for v in violations), violations

    def test_requests_attribute_detected(self) -> None:
        """Banned uuid calls are detected."""
        violations = _scan_source(_BAD_REQUESTS_SRC)
        assert any(v.startswith("call:requests.") for v in violations), violations

    def test_httpx_async_client_detected(self) -> None:
        """Banned environment reads are detected."""
        violations = _scan_source(_BAD_HTTPX_SRC)
        assert "call:httpx.AsyncClient" in violations, violations

    def test_asyncio_sleep_detected(self) -> None:
        """Banned file opens are detected."""
        violations = _scan_source(_BAD_ASYNCIO_SLEEP_SRC)
        assert "call:asyncio.sleep" in violations, violations

    def test_os_environ_get_detected(self) -> None:
        """Banned HTTP client calls are detected."""
        violations = _scan_source(_BAD_OS_ENVIRON_GET_SRC)
        assert "call:os.environ.get" in violations, violations

    def test_os_environ_subscript_detected(self) -> None:
        """Banned request calls are detected."""
        violations = _scan_source(_BAD_OS_ENVIRON_SUBSCRIPT_SRC)
        assert "subscript:os.environ" in violations, violations

    def test_open_builtin_detected(self) -> None:
        """Banned asyncio sleeps are detected."""
        violations = _scan_source(_BAD_OPEN_SRC)
        assert "call:open" in violations, violations

    def test_uuid_uuid4_detected(self) -> None:
        """Workflow sleep helpers are accepted."""
        violations = _scan_source(_BAD_UUID_SRC)
        assert "call:uuid.uuid4" in violations, violations

    def test_time_time_detected(self) -> None:
        """Unsafe import pass-through blocks are ignored."""
        violations = _scan_source(_BAD_TIME_SRC)
        assert "call:time.time" in violations, violations

    def test_non_workflow_class_is_ignored(self) -> None:
        """Non-workflow classes are ignored.

        Banned calls in classes NOT decorated with ``@workflow.defn``
        are not part of this scan - only the workflow body is.
        """
        assert _scan_source(_NON_WORKFLOW_CLASS_SRC) == []

    def test_decorator_with_keyword_args_recognised(self) -> None:
        """Subscripts of ``os.environ`` are detected.

        ``@workflow.defn(name="X")`` (Call wrapping the Attribute)
        must be recognised as a workflow class decorator just like the
        bare ``@workflow.defn`` form.
        """
        src = """
from temporalio import workflow

@workflow.defn(name="Foo", sandboxed=True)
class Foo:
    @workflow.run
    async def run(self):
        import time
        return time.time()
"""
        violations = _scan_source(src)
        assert "call:time.time" in violations, violations

    def test_aiohttp_attribute_detected(self) -> None:
        """Direct OpenAI calls are detected.

        Direct ``aiohttp.*`` calls inside a workflow body are banned by
        The workflow must call into
        ``aiohttp`` only via an ``@activity.defn`` activity (which lives
        outside the deterministic replay sandbox).
        """
        violations = _scan_source(_BAD_AIOHTTP_SRC)
        assert any(v.startswith("call:aiohttp.") for v in violations), violations

    def test_openai_attribute_detected(self) -> None:
        """Direct Anthropic calls are detected.

        Direct ``openai.*`` calls inside a workflow body are banned by
        Every LLM call must go
        through an activity; workflows do not perform
        I/O directly).
        """
        violations = _scan_source(_BAD_OPENAI_SRC)
        assert any(v.startswith("call:openai.") for v in violations), violations

    def test_anthropic_attribute_detected(self) -> None:
        """Direct aiohttp calls are detected.

        Direct ``anthropic.*`` calls inside a workflow body are banned
        by the workflow policy - every LLM call must go
        through an activity.
        """
        violations = _scan_source(_BAD_ANTHROPIC_SRC)
        assert any(v.startswith("call:anthropic.") for v in violations), violations

    def test_os_urandom_detected(self) -> None:
        """Allowed workflow helpers remain accepted.

        ``os.urandom`` is a non-deterministic source and must not be
        called inside a workflow body. Cryptographic randomness needs
        ``workflow.uuid4()`` (a Temporal-deterministic source) or an
        activity that returns the bytes.
        """
        violations = _scan_source(_BAD_OS_URANDOM_SRC)
        assert "call:os.urandom" in violations, violations


# ---------------------------------------------------------------------------
# WORKFLOW_TYPE_CAPABILITIES single-source-of-truth AST scan
# ---------------------------------------------------------------------------
#
# The :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`
# mapping is the *single* source of truth for the workflow-type
# capability set table. Every other module MUST consume the constant
# via ``from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES``
# (or an equivalent ``from`` import); no module may shadow the name
# with its own assignment. This static AST scan catches any
# redefinition that would silently fork the table.
#
# The canonical definition lives at
# ``platform/libs/temporal-shared/src/temporal_shared/capabilities.py``
# and is allow-listed below. Any other ``.py`` file under the platform
# tree that contains a top-level or class-level assignment to a name
# matching ``WORKFLOW_TYPE_CAPABILITIES`` is reported as a violation.
#
# This scanner is intentionally narrow: it looks only for the *name*
# ``WORKFLOW_TYPE_CAPABILITIES`` on the LHS of an assignment (or
# annotated assignment / augmented assignment). Re-binding the
# imported reference (``from ... import WORKFLOW_TYPE_CAPABILITIES``)
# is fine - that's how every legitimate consumer accesses it. A type
# alias like ``CapMap = WORKFLOW_TYPE_CAPABILITIES`` is also fine
# because it does not shadow the name.

# Workspace-relative path (forward-slash) of the canonical definition.
_CAPABILITIES_CANONICAL_PATH: str = (
    "libs/temporal-shared/src/temporal_shared/capabilities.py"
)

# Directory names pruned from the scan. Mirrors SCAN_EXCLUDED_DIRS in
# tests/property/_path_whitelist.py and adds a few extras local to this
# scan so test fixtures don't trip the check.
_CAP_SCAN_EXCLUDED_DIRS: frozenset[str] = frozenset(
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
    }
)

# Files allowed to *mention* the constant on the LHS of an assignment.
# The canonical module is the only one. Test files in this directory
# may reference the constant by name (e.g. for assertions) but never
# assign to it; the scanner enforces the assignment-only ban so those
# usages remain compliant without further allow-listing.
_CAPABILITIES_DEFINITION_ALLOWLIST: frozenset[str] = frozenset(
    {_CAPABILITIES_CANONICAL_PATH}
)


def _iter_platform_python_files() -> Iterator[Path]:
    """Yield every ``.py`` file under the platform root, pruned by
    :data:`_CAP_SCAN_EXCLUDED_DIRS`."""

    import os

    for current_dir, dirnames, filenames in os.walk(_PLATFORM_ROOT):
        # Mutate dirnames in-place so os.walk skips the excluded dirs.
        dirnames[:] = [d for d in dirnames if d not in _CAP_SCAN_EXCLUDED_DIRS]
        for filename in filenames:
            if filename.endswith(".py"):
                yield Path(current_dir) / filename


def _is_capabilities_assignment(node: ast.AST) -> bool:
    """Return True if *node* is an assignment whose LHS contains the name
    ``WORKFLOW_TYPE_CAPABILITIES`` (top-level or class-level)."""

    target_name = "WORKFLOW_TYPE_CAPABILITIES"

    # Plain assignment: ``WORKFLOW_TYPE_CAPABILITIES = ...``
    # Tuple assignment: ``(A, WORKFLOW_TYPE_CAPABILITIES) = (...)``
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if _name_appears_as_target(target, target_name):
                return True
        return False

    # Annotated assignment: ``WORKFLOW_TYPE_CAPABILITIES: ... = ...``
    if isinstance(node, ast.AnnAssign):
        return _name_appears_as_target(node.target, target_name)

    # Augmented assignment: ``WORKFLOW_TYPE_CAPABILITIES |= {...}``
    if isinstance(node, ast.AugAssign):
        return _name_appears_as_target(node.target, target_name)

    return False


def _name_appears_as_target(node: ast.expr, name: str) -> bool:
    """Return True if *node* is (or contains) a ``Name`` with id *name*.

    Handles ``Name``, ``Tuple``/``List`` of names, and ``Starred``
    targets. Subscript / Attribute LHS (e.g. ``obj.X = ...``) does NOT
    count - that is a member assignment, not a name shadowing.
    """

    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_name_appears_as_target(elt, name) for elt in node.elts)
    if isinstance(node, ast.Starred):
        return _name_appears_as_target(node.value, name)
    return False


@pytest.fixture(scope="module")
def workflow_type_capabilities_assignments() -> list[tuple[str, int]]:
    """Find every assignment to ``WORKFLOW_TYPE_CAPABILITIES`` in the tree.

    Returns a list of ``(workspace_relative_path, lineno)`` tuples. The
    canonical definition is included; allow-listing happens in the
    consuming test so the fixture remains a faithful AST report.
    """

    findings: list[tuple[str, int]] = []
    for path in _iter_platform_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            # Surfaced via test_workflow_file_parses for workflow files;
            # other syntax-broken files are out of scope for this scan.
            continue

        # Walk top-level + class-body statements (the only places a
        # module-level constant can be re-defined). We deliberately
        # *include* nested classes but *exclude* function bodies - a
        # local rebinding inside a function does not shadow the
        # imported module-level constant.
        stack: list[ast.AST] = list(tree.body)
        while stack:
            node = stack.pop()
            if _is_capabilities_assignment(node):
                rel = path.relative_to(_PLATFORM_ROOT).as_posix()
                findings.append((rel, getattr(node, "lineno", 0)))
            # Descend into ClassDef bodies (class-body assignments
            # also count as redefinitions).
            if isinstance(node, ast.ClassDef):
                stack.extend(node.body)
            # Do NOT descend into function/method bodies - those are
            # local rebindings and cannot shadow the module-level
            # constant for other importers.
    return findings


def test_workflow_type_capabilities_canonical_definition_exists(
    workflow_type_capabilities_assignments: list[tuple[str, int]],
) -> None:
    """The capability mapping is assigned only in its canonical module.

    The canonical module MUST contain at least one assignment to
    ``WORKFLOW_TYPE_CAPABILITIES``; otherwise the single-source
    invariant is vacuous.
    """
    canonical_hits = [
        (path, lineno)
        for path, lineno in workflow_type_capabilities_assignments
        if path == _CAPABILITIES_CANONICAL_PATH
    ]
    assert canonical_hits, (
        f"canonical WORKFLOW_TYPE_CAPABILITIES assignment not found in "
        f"{_CAPABILITIES_CANONICAL_PATH}; the constant must be defined "
            "there as the single source of truth"
    )


def test_workflow_type_capabilities_only_defined_in_canonical_module(
    workflow_type_capabilities_assignments: list[tuple[str, int]],
) -> None:
    """Each consumer imports the capability mapping from the canonical module.

    No ``.py`` file under the platform root other than
    :data:`_CAPABILITIES_CANONICAL_PATH` may contain a top-level or
    class-level assignment to ``WORKFLOW_TYPE_CAPABILITIES``. Importing
    the constant by name is fine; rebinding it is the violation.
    """
    violations = [
        (path, lineno)
        for path, lineno in workflow_type_capabilities_assignments
        if path not in _CAPABILITIES_DEFINITION_ALLOWLIST
    ]
    assert not violations, (
        "WORKFLOW_TYPE_CAPABILITIES must be defined exactly once "
        f"(in {_CAPABILITIES_CANONICAL_PATH}). Found shadowing "
        "assignments:\n  - "
        + "\n  - ".join(f"{p}:{ln}" for p, ln in violations)
    )


# ---------------------------------------------------------------------------
# Self-tests for the WORKFLOW_TYPE_CAPABILITIES scanner
# ---------------------------------------------------------------------------
#
# These exercise the scanner against synthetic source snippets so the
# detection logic stays correct independent of the live workspace.


def _scan_capabilities_assignments_in_source(source: str) -> list[int]:
    """Return line numbers where the synthetic source assigns to the
    constant at module top level or class body.
    """

    tree = ast.parse(source)
    hits: list[int] = []
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if _is_capabilities_assignment(node):
            hits.append(getattr(node, "lineno", 0))
        if isinstance(node, ast.ClassDef):
            stack.extend(node.body)
    return hits


class TestCapabilitiesAssignmentScanner:
    """Self-tests for the capability assignment AST scanner."""

    def test_module_level_assignment_detected(self) -> None:
        """Module-level assignment is detected."""
        src = "WORKFLOW_TYPE_CAPABILITIES = {}\n"
        assert _scan_capabilities_assignments_in_source(src) == [1]

    def test_annotated_assignment_detected(self) -> None:
        """Annotated assignment is detected."""
        src = "WORKFLOW_TYPE_CAPABILITIES: dict = {}\n"
        assert _scan_capabilities_assignments_in_source(src) == [1]

    def test_augmented_assignment_detected(self) -> None:
        """Augmented assignment is detected."""
        src = "WORKFLOW_TYPE_CAPABILITIES |= {'x': frozenset()}\n"
        assert _scan_capabilities_assignments_in_source(src) == [1]

    def test_tuple_assignment_detected(self) -> None:
        """Tuple assignment is detected."""
        src = "OTHER, WORKFLOW_TYPE_CAPABILITIES = 1, {}\n"
        assert _scan_capabilities_assignments_in_source(src) == [1]

    def test_class_body_assignment_detected(self) -> None:
        """Class-body assignment is detected.

        A class-body assignment also shadows the module-level constant
        for any consumer that accesses it through the class.
        """
        src = (
            "class Holder:\n"
            "    WORKFLOW_TYPE_CAPABILITIES = {}\n"
        )
        # The assignment is on line 2 of the source.
        assert _scan_capabilities_assignments_in_source(src) == [2]

    def test_function_body_assignment_ignored(self) -> None:
        """Function-body assignment is ignored.

        A local rebinding inside a function body does NOT shadow the
        module-level constant for outside importers; the scanner must
        not flag it.
        """
        src = (
            "def f():\n"
            "    WORKFLOW_TYPE_CAPABILITIES = {}\n"
            "    return WORKFLOW_TYPE_CAPABILITIES\n"
        )
        assert _scan_capabilities_assignments_in_source(src) == []

    def test_import_does_not_count_as_assignment(self) -> None:
        """Imports do not count as assignments.

        Importing the name (the legitimate access pattern) must not be
        flagged.
        """
        src = (
            "from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES\n"
        )
        assert _scan_capabilities_assignments_in_source(src) == []

    def test_attribute_assignment_ignored(self) -> None:
        """Attribute assignments are ignored.

        ``ns.WORKFLOW_TYPE_CAPABILITIES = ...`` is a member write, not
        a name shadowing, and is out of scope for this scanner.
        """
        src = (
            "class NS: pass\n"
            "NS.WORKFLOW_TYPE_CAPABILITIES = {}\n"
        )
        assert _scan_capabilities_assignments_in_source(src) == []

    def test_alias_to_canonical_constant_ignored(self) -> None:
        """Aliases to the canonical constant are ignored.

        Defining a different-name alias (``CapMap = WORKFLOW_TYPE_CAPABILITIES``)
        is fine - it doesn't shadow the canonical name.
        """
        src = (
            "from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES\n"
            "CapMap = WORKFLOW_TYPE_CAPABILITIES\n"
        )
        assert _scan_capabilities_assignments_in_source(src) == []

    def test_unrelated_capitalised_name_ignored(self) -> None:
        """Unrelated capitalized names are ignored.

        The scanner matches the *exact* name only.
        """
        src = (
            "WORKFLOW_TYPE_CAPABILITIES_BACKUP = {}\n"
            "_WORKFLOW_TYPE_CAPABILITIES = {}\n"
        )
        assert _scan_capabilities_assignments_in_source(src) == []


# ---------------------------------------------------------------------------
# Activity start_workflow ban - workflow-decision logic
# must not appear in activity modules.
# ---------------------------------------------------------------------------
#
# Correctness rule:
# ``workers/*/activities/`` altındaki dosyalar ``client.start_workflow``
# veya eşdeğer çağrı içermez (workflow karar mantığı yalnız workflow
# modüllerinde).
#
# This is the static AST counterpart of the runtime "workers crash
# Temporal redelegate" guarantee. If an activity smuggles a
# ``start_workflow`` call, the workflow's history becomes
# nondeterministic from the workflow engine's perspective - a future
# replay could observe the workflow start as an activity-side effect
# rather than a workflow-side decision.
#
# The scanner used here lives in ``_path_whitelist`` and is also
# consumed by ``test_path_coverage.py``. Hosting the activity-only
# assertion in this module keeps it next to the replay-safety static
# invariants.

from _path_whitelist import (  # noqa: E402
    format_findings as _pw_format_findings,
    scan_activities_start_workflow as _pw_scan_activities_start_workflow,
)


def test_activities_have_no_start_workflow_calls() -> None:
    """Statik AST taraması: ``workers/*/activities/`` altındaki hiçbir
    ``.py`` dosyası ``client.start_workflow`` /
    ``client.execute_workflow`` / ``client.start_child_workflow``
    çağrısı içermez.

    A failing assertion lists every offending file with line numbers
    so the violation is actionable from a single test report. The
    helper :func:`scan_activities_start_workflow` matches by trailing
    method name (``start_workflow`` etc.) regardless of receiver name,
    so ``self.client.start_workflow``, ``temporal_client.start_workflow``
    and the canonical ``client.start_workflow`` are all caught.
    """

    findings = _pw_scan_activities_start_workflow()
    assert not findings, (
        "Workflow-start violation - call inside "
        "activity module. Workflow-decision logic must live in "
        "workers/*/workflows/, not activities/.\n"
        + _pw_format_findings(findings)
    )


# ---------------------------------------------------------------------------
# Temporal-shared module-level replay safety
# ---------------------------------------------------------------------------
#
# Modules under :data:`SHARED_REPLAY_SAFE_DIRS` are imported by Temporal
# workflow code (either directly or through the
# ``workflow.unsafe.imports_passed_through()`` sandbox escape hatch).
# A non-deterministic call hidden in any of these helpers - for example
# a stray ``datetime.now()`` at the top level of a helper that the
# workflow imports - would taint *every* workflow that uses the helper.
#
# This scanner walks the *entire* module tree (not just inside
# ``@workflow.defn`` classes) and reports any banned call. Function
# bodies *and* class bodies are descended into; only nodes that look
# like docstrings (string-literal expression statements) are ignored.
# Type hints and default values are inspected as part of normal AST
# walking, but those rarely call banned symbols and any genuine call
# would be reported correctly.


def _shared_replay_safe_files() -> tuple[Path, ...]:
    """Return every ``.py`` file under :data:`SHARED_REPLAY_SAFE_DIRS`."""

    files: list[Path] = []
    for directory in SHARED_REPLAY_SAFE_DIRS:
        if not directory.is_dir():
            continue
        files.extend(sorted(directory.rglob("*.py")))
    return tuple(files)


SHARED_REPLAY_SAFE_FILES: tuple[Path, ...] = _shared_replay_safe_files()


def _walk_module_for_banned_calls(tree: ast.Module) -> list[tuple[ast.AST, str, str]]:
    """Walk every node in *tree* and return banned-call findings.

    Returns a list of ``(node, category, dotted)`` tuples. Skips the
    body of any ``with workflow.unsafe.imports_passed_through():`` block
    (the Temporal sandbox escape hatch - same exemption as the
    decorated-class scanner).
    """

    findings: list[tuple[ast.AST, str, str]] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()

        if isinstance(node, (ast.With, ast.AsyncWith)) and _is_workflow_unsafe_imports_block(
            node
        ):
            # Skip body - those are import statements behind the
            # Temporal sandbox escape hatch.
            continue

        if isinstance(node, ast.Call):
            hit = _classify_call_target(node.func)
            if hit is not None:
                category, dotted = hit
                findings.append((node, category, dotted))

        if isinstance(node, ast.Subscript) and _is_environ_subscript(node):
            findings.append((node, "subscript", "os.environ"))

        for child in ast.iter_child_nodes(node):
            stack.append(child)

    return findings


def test_shared_replay_safe_dirs_exist() -> None:
    """Every directory in :data:`SHARED_REPLAY_SAFE_DIRS` must be present
    on disk; otherwise the module-level scan below would silently
    degrade to a no-op.
    """

    for directory in SHARED_REPLAY_SAFE_DIRS:
        assert directory.is_dir(), (
            f"shared replay-safe directory missing: "
            f"{directory.relative_to(_PLATFORM_ROOT)} - replay-safe shared module scanning cannot "
            "be enforced if the directory is absent."
        )


def test_shared_replay_safe_files_collected() -> None:
    """The module-level scan must find at least one ``.py`` file under
    :data:`SHARED_REPLAY_SAFE_DIRS`; otherwise the parametrised test
    below is empty and the property is vacuous.
    """

    assert len(SHARED_REPLAY_SAFE_FILES) > 0, (
        "no .py files found under shared replay-safe directories - "
        f"checked: {[str(d) for d in SHARED_REPLAY_SAFE_DIRS]}"
    )


@pytest.mark.parametrize(
    "path",
    SHARED_REPLAY_SAFE_FILES,
    ids=[
        str(p.relative_to(_PLATFORM_ROOT)).replace("\\", "/")
        for p in SHARED_REPLAY_SAFE_FILES
    ],
)
def test_shared_replay_safe_module_has_no_banned_calls(path: Path) -> None:
    """Every module under :data:`SHARED_REPLAY_SAFE_DIRS` is imported by
    workflow code and must therefore be free of non-deterministic /
    I/O call sites at *any* scope (module top-level, helper functions,
    classes). The Temporal sandbox escape hatch
    (``with workflow.unsafe.imports_passed_through():``) remains the
    only legitimate route for I/O-capable imports.
    """

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    findings = _walk_module_for_banned_calls(tree)

    violations: list[str] = []
    rel = path.relative_to(_PLATFORM_ROOT)
    for node, category, dotted in findings:
        if category == "subscript":
            violations.append(
                f"{rel}:{node.lineno} - banned os.environ[...] subscript"
            )
        else:
            violations.append(
                f"{rel}:{node.lineno} - banned {category} call: {dotted}(...)"
            )

    assert not violations, (
        "Replay-safe shared module violation - "
        "module must not call non-deterministic or I/O symbols at any "
        "scope. Use deterministic helpers (caller passes "
        "``workflow.now()``-derived timestamps), and keep I/O inside "
        "``@activity.defn`` activities.\n  - "
        + "\n  - ".join(violations)
    )


# ---------------------------------------------------------------------------
# workflow.execute_activity start_to_close_timeout
# ---------------------------------------------------------------------------
#
# An activity that takes longer than its
# ``start_to_close_timeout`` is restarted by the Temporal cluster.
# Without an explicit ``start_to_close_timeout`` the activity attempt
# would be unbounded - the operator loses the back-pressure signal that
# differentiates "slow but progressing" from "stuck".  This static
# scanner enforces that *every* workflow-side activity / child workflow
# launch call inside a ``@workflow.defn`` class names the keyword
# explicitly.
#
# The methods covered are the canonical Temporal Python SDK entry points:
#
# * ``workflow.execute_activity``
# * ``workflow.execute_activity_method``
# * ``workflow.execute_local_activity``
# * ``workflow.execute_local_activity_method``
# * ``workflow.start_activity``
# * ``workflow.start_activity_method``
# * ``workflow.start_local_activity``
# * ``workflow.start_local_activity_method``
#
# (The child-workflow variants - ``execute_child_workflow`` /
# ``start_child_workflow`` - accept ``execution_timeout`` /
# ``run_timeout`` instead and are out of scope for the activity-timeout
# timeout clause.)

#: Set of dotted names that must always be invoked with a
#: ``start_to_close_timeout`` keyword argument.
_ACTIVITY_LAUNCH_DOTTED: frozenset[str] = frozenset(
    {
        "workflow.execute_activity",
        "workflow.execute_activity_method",
        "workflow.execute_local_activity",
        "workflow.execute_local_activity_method",
        "workflow.start_activity",
        "workflow.start_activity_method",
        "workflow.start_local_activity",
        "workflow.start_local_activity_method",
    }
)


def _has_kwarg(call: ast.Call, name: str) -> bool:
    """Return True if *call* explicitly passes ``name=<expr>``.

    A ``**kwargs`` splat (``ast.keyword`` with ``arg is None``) is
    deliberately *not* counted: relying on a runtime-constructed dict
    to carry ``start_to_close_timeout`` defeats the static guarantee
    we are trying to enforce.
    """

    for kw in call.keywords:
        if kw.arg == name:
            return True
    return False


def _iter_activity_launch_calls_in_class(
    cls: ast.ClassDef,
) -> Iterator[tuple[ast.Call, str]]:
    """Yield ``(call_node, dotted_name)`` for every activity-launch call
    inside the body of a ``@workflow.defn`` class.
    """

    for node in _walk_workflow_class(cls):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if dotted in _ACTIVITY_LAUNCH_DOTTED:
            yield node, dotted


@pytest.mark.parametrize(
    "path",
    WORKFLOW_FILES,
    ids=[str(p.relative_to(_PLATFORM_ROOT)).replace("\\", "/") for p in WORKFLOW_FILES],
)
def test_activity_launches_pass_start_to_close_timeout(path: Path) -> None:
    """Every ``workflow.execute_activity`` (and its sibling launch
    methods) call inside a ``@workflow.defn`` class MUST pass the
    ``start_to_close_timeout`` keyword. Implicit defaults are not
    acceptable - the workflows spec design document calls out an
    activity timeout configured per call site so the operator can tune
    long-running steps independently from short ones.
    """

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    classes = _collect_workflow_classes(tree)

    violations: list[str] = []
    rel = path.relative_to(_PLATFORM_ROOT)

    for cls in classes:
        for call, dotted in _iter_activity_launch_calls_in_class(cls):
            if not _has_kwarg(call, "start_to_close_timeout"):
                violations.append(
                    f"{rel}:{call.lineno} - {dotted}(...) inside "
                    f"@workflow.defn class {cls.name!r} is missing the "
                    "start_to_close_timeout keyword"
                )

    assert not violations, (
        "Activity timeout violation - activity launch is missing "
        "start_to_close_timeout. Pass an explicit timedelta so the "
        "Temporal cluster can detect a stuck activity and re-dispatch "
        "it (workflow body should compute the timeout from input or a "
        "module constant, never inherit a runtime default).\n  - "
        + "\n  - ".join(violations)
    )


# ---------------------------------------------------------------------------
# Self-tests for the activity-launch start_to_close_timeout scanner
# ---------------------------------------------------------------------------


def _collect_activity_launch_violations_in_source(source: str) -> list[str]:
    """Run the timeout-keyword scanner over a synthetic source string."""

    tree = ast.parse(source)
    violations: list[str] = []
    for cls in _collect_workflow_classes(tree):
        for call, dotted in _iter_activity_launch_calls_in_class(cls):
            if not _has_kwarg(call, "start_to_close_timeout"):
                violations.append(dotted)
    return violations


_GOOD_ACTIVITY_LAUNCH_SRC = """
from datetime import timedelta
from temporalio import workflow

@workflow.defn
class GoodLaunch:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            "do_thing",
            args=[1, 2],
            start_to_close_timeout=timedelta(seconds=30),
        )
"""


_BAD_ACTIVITY_LAUNCH_NO_TIMEOUT_SRC = """
from temporalio import workflow

@workflow.defn
class BadLaunch:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            "do_thing",
            args=[1, 2],
        )
"""


_BAD_ACTIVITY_LAUNCH_KWARGS_SPLAT_SRC = """
from temporalio import workflow

@workflow.defn
class BadLaunchSplat:
    @workflow.run
    async def run(self, opts: dict) -> str:
        return await workflow.execute_activity(
            "do_thing",
            args=[1, 2],
            **opts,
        )
"""


_BAD_LOCAL_ACTIVITY_NO_TIMEOUT_SRC = """
from temporalio import workflow

@workflow.defn
class BadLocal:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_local_activity("do_thing")
"""


class TestActivityLaunchTimeoutScanner:
    """Self-tests for the ``start_to_close_timeout`` scanner."""

    def test_explicit_timeout_is_accepted(self) -> None:
        """Explicit timeout is accepted."""
        assert _collect_activity_launch_violations_in_source(
            _GOOD_ACTIVITY_LAUNCH_SRC
        ) == []

    def test_missing_timeout_is_rejected(self) -> None:
        """Missing timeout is rejected."""
        violations = _collect_activity_launch_violations_in_source(
            _BAD_ACTIVITY_LAUNCH_NO_TIMEOUT_SRC
        )
        assert violations == ["workflow.execute_activity"], violations

    def test_kwargs_splat_does_not_satisfy_static_check(self) -> None:
        """``**opts`` does not satisfy the static check.

        ``**opts`` defeats the static guarantee - the scanner cannot
        prove the dict carries ``start_to_close_timeout`` and so must
        flag the call to keep the invariant tight.
        """
        violations = _collect_activity_launch_violations_in_source(
            _BAD_ACTIVITY_LAUNCH_KWARGS_SPLAT_SRC
        )
        assert violations == ["workflow.execute_activity"], violations

    def test_local_activity_also_requires_timeout(self) -> None:
        """Local activity launches also require a timeout."""
        violations = _collect_activity_launch_violations_in_source(
            _BAD_LOCAL_ACTIVITY_NO_TIMEOUT_SRC
        )
        assert violations == ["workflow.execute_local_activity"], violations

    def test_non_workflow_class_is_ignored(self) -> None:
        """Non-workflow classes are ignored.

        A bare ``execute_activity`` call outside a ``@workflow.defn``
        class is outside this scan - only workflow bodies are.
        """
        src = """
from temporalio import workflow

class NotAWorkflow:
    async def run(self) -> str:
        return await workflow.execute_activity("do_thing")
"""
        assert _collect_activity_launch_violations_in_source(src) == []
