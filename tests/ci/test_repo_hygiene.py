"""CI gate - repo hygiene.

Some development sessions accidentally redirected Python REPL output
(`type(x).__name__`, `dir(x)`, ad-hoc print statements) to disk and
left files literally named ``{key``, ``0,``, ``bytes``, ``dict[str``,
``None``, ``set()`` or ``str`` checked into the services tree. This gate
keeps the repository clean by failing CI the moment any of those names
reappear anywhere in the workspace tree.

We deliberately match only file *basenames* (not directory names) so
legitimate package directories such as ``services/``, ``src/`` and
``str/`` are not flagged, and legitimate Python source files such as
``dict_utils.py`` or ``none_test.py`` keep their extensions and
therefore never match the bare-identifier patterns.

The walker prunes the same vendor / cache / build trees as
``tests/property/test_out_of_scope_paths.py`` so we do not hand-roll
yet another exclusion list.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

import pytest

# ``conftest.py`` lives two directories up. Pytest auto-loads it when
# invoked from the workspace root, but we add ``tests/`` to ``sys.path``
# defensively so this file is also runnable via
# ``python -m pytest tests/ci``.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import WORKSPACE_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Forbidden basename patterns
# ---------------------------------------------------------------------------

#: Forbidden file basenames. Each entry is an ``fnmatch`` pattern
#: applied against the leaf filename only. The list covers accidental
#: Python type-hint leakage:
#:
#: * ``{*``         - anything starting with ``{`` (``{key``,
#:                    ``{value: int}``, …).
#: * ``0,``         - literal name ``0,`` (REPL tuple-printing).
#: * ``bytes``      - literal name ``bytes`` with no extension
#:                    (``type(x).__name__`` redirected to disk).
#: * ``dict[*``     - anything starting with ``dict[`` (``dict[str``,
#:                    ``dict[str, Any]``, …).
#: * ``list[*``     - same family as ``dict[…``; future-proofs the gate
#:                    against the closely-related leakage.
#: * ``None``       - literal name ``None`` with no extension.
#: * ``set()``      - literal name ``set()`` with no extension.
#: * ``str``        - literal name ``str`` with no extension.
#:
#: Matching is done with ``fnmatch.fnmatchcase`` against the basename so
#: extensions matter: a file named ``str.py`` is *not* flagged.
_FORBIDDEN_BASENAME_PATTERNS: tuple[str, ...] = (
    "{*",
    "0,",
    "bytes",
    "dict[*",
    "list[*",
    "None",
    "set()",
    "str",
)

#: Directories pruned during the recursive walk. Mirrors the exclusion
#: set in ``tests/property/test_out_of_scope_paths.py`` so vendored
#: trees, virtual envs, build outputs and tooling caches are not
#: scanned. ``atlassian_mcp_bitbucket`` is pruned with the other
#: service-owned dependency trees.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        ".git",
        ".next",
        "atlassian_mcp_bitbucket",
        ".hypothesis",
        ".pytest_cache",
        "__pycache__",
        "dist",
        "build",
        ".mypy_cache",
        ".ruff_cache",
        ".turbo",
        ".forge",
    }
)


def _basename_matches_any(name: str, patterns: tuple[str, ...]) -> str | None:
    """Return the first pattern in ``patterns`` matching ``name``, else ``None``.

    ``fnmatch.fnmatchcase`` is used so the check is case-sensitive and
    portable across POSIX and Windows hosts (Windows ``fnmatch`` is
    case-insensitive by default; ``fnmatchcase`` overrides that).
    """

    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return pattern
    return None


def _walk_repo_files(root: Path) -> list[Path]:
    """Return every regular file under ``root``, pruning vendor trees.

    Symlinks are *not* followed: that avoids accidental traversal into
    out-of-tree directories on Windows-style junction points.
    """

    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in-place so ``os.walk`` skips the excluded subtrees.
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        for fname in filenames:
            matches.append(Path(dirpath) / fname)
    return matches


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern", _FORBIDDEN_BASENAME_PATTERNS, ids=list(_FORBIDDEN_BASENAME_PATTERNS)
)
def test_no_forbidden_basenames_anywhere(pattern: str) -> None:
    """No Python type-hint leakage filenames remain.

    For each forbidden basename pattern we walk the workspace tree and
    assert there is no file whose leaf name matches it. The failure
    message lists the offending paths so the operator can ``rm`` them
    directly.
    """

    offenders: list[str] = []
    for path in _walk_repo_files(WORKSPACE_ROOT):
        if fnmatch.fnmatchcase(path.name, pattern):
            offenders.append(str(path.relative_to(WORKSPACE_ROOT)))

    assert not offenders, (
        f"Forbidden basename pattern {pattern!r} matched {len(offenders)} "
        f"file(s) under the workspace root. These are accidental Python "
        f"type-hint outputs (e.g. "
        f"``type(x).__name__``) redirected to disk and must be deleted. "
        f"Offending paths: {offenders}"
    )


def test_atlassian_mcp_bitbucket_subtree_has_no_venv_or_nested_git() -> None:
    """No ``.venv`` or worktree-style ``.git`` inside the MCP gateway.

    The ``services/atlassian_mcp_bitbucket/`` tree is the real MCP
    gateway. What MUST NOT live inside it are:

    * a ``.venv/`` directory (developer virtualenv accidentally
      committed),
    * a ``.git/`` worktree (nested git checkout / submodule artefact).

    This gate keeps them removed inside ``platform/``.
    """

    candidate_roots = (
        WORKSPACE_ROOT / "services" / "atlassian_mcp_bitbucket",
    )
    offenders: list[str] = []
    for root in candidate_roots:
        if not root.is_dir():
            continue
        for name in (".venv", ".git"):
            candidate = root / name
            if candidate.exists():
                offenders.append(str(candidate))

    assert not offenders, (
        "Forbidden artefacts found inside services/atlassian_mcp_bitbucket/: "
        f"{offenders}. ``.gitignore`` blocks "
        "``services/atlassian_mcp_bitbucket/.venv/``; "
        "if either path reappears the cleanup has regressed."
    )


def test_forbidden_basename_patterns_are_well_formed() -> None:
    """Self-test - every pattern is a non-empty string ``fnmatch`` can parse.

    A regression here (e.g. an empty string slipping into the tuple)
    would cause ``fnmatch.fnmatchcase('', '')`` to spuriously match
    every file. We pin the contract explicitly so the gate cannot
    silently degrade to a no-op.
    """

    assert _FORBIDDEN_BASENAME_PATTERNS, (
        "Forbidden basename pattern list is empty; the gate would "
        "no-op and accept any filename."
    )
    for pattern in _FORBIDDEN_BASENAME_PATTERNS:
        assert isinstance(pattern, str) and pattern, (
            f"Pattern {pattern!r} is not a non-empty string."
        )
        # ``fnmatch.translate`` raises if the pattern is malformed.
        fnmatch.translate(pattern)
