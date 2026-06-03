"""Static AST invariant: no hard-coded Jira ``customfield_*`` ids.



Static AST check: the platform code base MUST NOT embed Jira
custom-field id literals (``customfield_10001``,
``customfield_10049``,...) anywhere in the in-scope source tree.
Field ids vary across Jira tenants, so any literal embedded in code
becomes a deployment-blocker the moment a new tenant is onboarded.
The:class:`automation_service.jira_field_resolver.JiraFieldResolver`
is the single sanctioned mechanism for translating field display
names to ids at runtime; this test enforces that nobody short-circuits
it with a literal.

Scope
-----

The walker scans every ``.py`` file under three roots, all relative
to the platform workspace root:

* ``platform/services/automation-service/``
* ``platform/workers/``
* ``platform/libs/``

Files outside these roots (notably the
``platform/services/atlassian_mcp_bitbucket/`` MCP gateway) are NOT in
scope and legitimately reference field-id literals in their docstrings,
fixtures, and test data.

Whitelist
---------

Two classes of in-scope files are exempted:

1. **Test subtrees** — anything under a ``tests/`` directory inside
 the scope. Tests legitimately *assert against* field ids (e.g.
 invariant that pin a fixture's expected output) and we want
 that to keep working without registering every test file
 one-by-one. The convention "production code never references
 field ids; tests assert against them after a resolve" is what
 the rule enforces.
2. **The resolver module itself** — ``jira_field_resolver.py``
 mentions ``customfield_*`` only inside docstrings and example
 strings (never as a runtime constant). Allow-listing the file is
 simpler than threading "string is in a docstring" through the AST
 walker, and the file is small enough that human review can
 confirm it never *uses* a hard-coded literal.

Failure shape
-------------

When the walker finds an offending literal it collects a tuple of
``(workspace_relative_path, line, literal)`` for every occurrence
and fails with the full list. The list is sorted so test output is
stable across runs (which simplifies diffing CI failures).

Implementation notes
--------------------

* The walker descends through every ``.py`` file with:mod:`ast`,
 so f-strings (``f"customfield_{n}"``) are NOT flagged — those
 cannot match the ``^customfield_\\d+$`` regex once Python parses
 them into a ``JoinedStr`` whose ``Constant`` parts are
 ``"customfield_"`` (no digit suffix). This is intentional: a
 prefix-only f-string is harmless because it cannot resolve to a
 real id without a literal int already present elsewhere.
* The regex anchors with ``^`` and ``$`` so partial matches like
 ``"customfield_10020 is great"`` are NOT flagged either. The rule
 is specifically about *literal field ids*, not general mentions
 of the prefix.
* Standard tooling caches and vendored trees
 (``__pycache__``, ``.venv``, ``.pytest_cache``, ``.hypothesis``,
 ``.mypy_cache``, ``.ruff_cache``, ``node_modules``, ``dist``,
 ``build``, ``.git``) are pruned at the walk level so the test
 stays fast even on a populated repo.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Workspace anchors
# ---------------------------------------------------------------------------

# tests/property/test_no_hardcoded_field_ids.py → platform/
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Scope and whitelist
# ---------------------------------------------------------------------------

#: Roots whose ``.py`` files are scanned. Workspace-relative,
#: forward-slash style.
SCOPE_ROOTS: tuple[str, ...] = (
    "services/automation-service",
    "workers",
    "libs",
)

#: Directory names pruned at every level of the walk. Mirrors the
#: convention in:mod:`tests.property._path_whitelist` so this
#: scanner stays consistent with sibling invariant.
_PRUNED_DIRS: frozenset[str] = frozenset(
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

#: Path fragments (forward-slash) that mark a file as exempt from the
#: literal ban. A file is exempt iff *any* of these substrings appears
#: in its workspace-relative path.
#:
#: ``/tests/`` — test code legitimately asserts against field ids.
#: ``jira_field_resolver.py`` — the resolver module mentions the
#: prefix only in docstrings and the literal would not be a
#: hard-coded id even if it appeared. Allow-listing avoids any
#: docstring-extraction subtlety.
_WHITELIST_FRAGMENTS: tuple[str, ...] = (
    "/tests/",
    "jira_field_resolver.py",
)

#: The regex flags exact Jira field-id literals. Anchored at both ends so the
#: literal must be *exactly* ``customfield_<digits>`` — generic
#: mentions in surrounding prose do not match.
_FIELD_ID_RE: re.Pattern[str] = re.compile(r"^customfield_\d+$")


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------


def _normalise(path: Path) -> str:
    """Return *path* as a forward-slash workspace-relative string."""

    return path.relative_to(_PLATFORM_ROOT).as_posix()


def _iter_in_scope_files() -> Iterator[Path]:
    """Yield every ``.py`` file under:data:`SCOPE_ROOTS`, pruned."""

    for scope in SCOPE_ROOTS:
        scope_path = _PLATFORM_ROOT / scope
        if not scope_path.is_dir():
            # Surfaced separately by ``test_scope_roots_exist``.
            continue
        for dirpath, dirnames, filenames in os.walk(str(scope_path)):
            # Mutate ``dirnames`` in place so ``os.walk`` skips the
            # excluded subtrees.
            dirnames[:] = sorted(d for d in dirnames if d not in _PRUNED_DIRS)
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield Path(dirpath) / filename


def _is_whitelisted(rel_posix: str) -> bool:
    """Return True iff *rel_posix* contains any whitelist fragment."""

    for fragment in _WHITELIST_FRAGMENTS:
        if fragment in rel_posix:
            return True
    return False


# ---------------------------------------------------------------------------
# AST scan
# ---------------------------------------------------------------------------


def _scan_module(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Yield ``(lineno, literal)`` pairs for every offending Constant.

 Walks every:class:`ast.Constant` whose value is a ``str`` and
 matches:data:`_FIELD_ID_RE`. The match is exact-string (anchored)
 so f-string prefix fragments and partial mentions are excluded
 by construction.
 """

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _FIELD_ID_RE.match(node.value):
                yield (node.lineno, node.value)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scope_roots_exist() -> None:
    """Every directory in:data:`SCOPE_ROOTS` must exist.

 A missing root would silently skip the corresponding scan branch
 and let the property degrade to a no-op. Surfacing the absence
 as a hard failure keeps the test honest if the workspace layout
 ever changes.
 """

    missing: list[str] = []
    for scope in SCOPE_ROOTS:
        if not (_PLATFORM_ROOT / scope).is_dir():
            missing.append(scope)
    assert not missing, (
        "expected scope roots are missing under the platform workspace; "
        "the field-id literal scan cannot run without them.\n - "
        + "\n - ".join(missing)
    )


def test_at_least_one_python_file_in_scope() -> None:
    """Sanity check: the walker must find at least one ``.py`` file.

 Without this, an accidentally empty workspace would let the
 property pass vacuously.
 """

    files = list(_iter_in_scope_files())
    assert files, (
        "no.py files discovered under scope roots; "
        f"checked: {[str(_PLATFORM_ROOT / s) for s in SCOPE_ROOTS]}"
    )


def test_no_hardcoded_field_id_literals() -> None:
    """Scan in-scope Python files for hard-coded Jira field ids.

 AST-walks every in-scope ``.py`` file and asserts that no string
 literal matches ``^customfield_\\d+$``. Whitelisted files (test
 subtrees + the resolver module itself) are skipped at the path
 level. Failure reports the full list of
 ``(file, line, literal)`` triples so a contributor can fix the
 offending lines without further digging.
 """

    offences: list[tuple[str, int, str]] = []

    for path in _iter_in_scope_files():
        rel = _normalise(path)
        if _is_whitelisted(rel):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Skipping unreadable files is consistent with the sibling
            # scanners in ``tests/property/_path_whitelist.py``; the
            # CI lane fails earlier on encoding regressions.
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            # Non-parseable files are caught by the per-file
            # determinism scanners; this property is about literals,
            # not syntax.
            continue
        for lineno, literal in _scan_module(tree):
            offences.append((rel, lineno, literal))

    # Stable ordering for diff-friendly CI output.
    offences.sort()

    assert not offences, (
        "Jira custom-field ids must NOT be "
        "hard-coded in the in-scope source tree. Use "
        "automation_service.jira_field_resolver.JiraFieldResolver to "
        "translate field display names at runtime.\n - "
        + "\n - ".join(
            f"{path}:{lineno} — {literal!r}"
            for path, lineno, literal in offences
        )
    )


# ---------------------------------------------------------------------------
# Self-checks for the scanner
# ---------------------------------------------------------------------------


class TestScannerSelfChecks:
    """Lock in the regex / AST scanner against synthetic snippets.

 The production-tree test above is only as strong as the scanner
 that powers it. These self-checks make sure the regex and the
 AST walk catch the right shapes (and reject the wrong ones).
 """

    def test_exact_field_literal_detected(self) -> None:
        src = '_FIELD = "customfield_10020"\n'
        tree = ast.parse(src)
        assert list(_scan_module(tree)) == [(1, "customfield_10020")]

    def test_exact_field_literal_in_dict_value(self) -> None:
        src = 'FIELDS = {"sprint": "customfield_10020"}\n'
        tree = ast.parse(src)
        assert (1, "customfield_10020") in list(_scan_module(tree))

    def test_partial_match_not_flagged(self) -> None:
        """Mentions in surrounding prose are not field-id literals."""

        src = '_DOC = "customfield_10020 is the Sprint field id"\n'
        tree = ast.parse(src)
        assert list(_scan_module(tree)) == []

    def test_prefix_only_not_flagged(self) -> None:
        """The bare prefix without digits is not a hard-coded id."""

        src = '_PREFIX = "customfield_"\n'
        tree = ast.parse(src)
        assert list(_scan_module(tree)) == []

    def test_fstring_prefix_not_flagged(self) -> None:
        """F-string fragments are pure prefixes after ast.parse."""

        src = '_RUNTIME = f"customfield_{n}"\n'
        tree = ast.parse(src)
        assert list(_scan_module(tree)) == []

    def test_uppercase_not_flagged(self) -> None:
        """The Jira convention is lowercase ``customfield_``; the
 upper-case variant is not a real id and must not match."""

        src = '_X = "CustomField_10020"\n'
        tree = ast.parse(src)
        assert list(_scan_module(tree)) == []

    def test_multiple_literals_collected_in_order(self) -> None:
        src = (
            '_A = "customfield_10001"\n'
            '_B = "customfield_10049"\n'
            '_C = "customfield_99999"\n'
        )
        tree = ast.parse(src)
        results = list(_scan_module(tree))
        assert results == [
            (1, "customfield_10001"),
            (2, "customfield_10049"),
            (3, "customfield_99999"),
        ]


class TestWhitelistFiltering:
    """Verify the whitelist fragment matcher."""

    @pytest.mark.parametrize(
        "rel_path",
        [
            "platform/services/automation-service/tests/unit/test_x.py",
            "workers/agent-runner-worker/tests/integration/test_y.py",
            "libs/temporal-shared/tests/property/test_z.py",
            "services/automation-service/src/automation_service/jira_field_resolver.py",
        ],
    )
    def test_whitelisted_paths_recognised(self, rel_path: str) -> None:
        assert _is_whitelisted(rel_path) is True

    @pytest.mark.parametrize(
        "rel_path",
        [
            "services/automation-service/src/automation_service/app.py",
            "workers/agent-runner-worker/src/activities/jira.py",
            "libs/mcp_client/src/mcp_client/atlassian_client.py",
        ],
    )
    def test_in_scope_paths_not_whitelisted(self, rel_path: str) -> None:
        assert _is_whitelisted(rel_path) is False
