"""Tests for out-of-scope artifact absence.

The project workspace must not contain artifacts that
belong to features explicitly deferred or excluded from the in-scope
deliverable set:

* ``helm/`` and ``k8s/`` (and the nested ``k8s/manifests/``) - Kubernetes
  deployment artifacts are out of scope.
* Any nested ``helm/`` directory anywhere under the workspace root
  is out of scope.
* Any KEDA ``ScaledObject``-style YAML or ``keda-*.yaml`` autoscaler
  manifests are out of scope.

Note: ``forge-app/`` was historically listed here as a backlog item
but now exists as an opt-in Forge add-on under ``platform/forge-app/``
gated by ``FEATURE_FLAG_FORGE_ADDON_ENABLED``. The directory is now an
in-scope artifact and has been removed from ``FORBIDDEN_PATHS`` accordingly.

The fixture ``FORBIDDEN_PATHS`` (defined in ``tests/conftest.py``)
encodes both literal paths and glob-style patterns. This test
parameterizes over each entry and asserts:

* For literal paths (no ``**`` segment), the path does not exist
  beneath ``WORKSPACE_ROOT``.
* For glob patterns (``**/<basename-pattern>``), the workspace tree -
  pruned of heavy / vendored directories such as ``node_modules/``,
  ``.venv/``, ``.git/``, ``.next/``, and ``atlassian_mcp_bitbucket/`` - yields
  no matching paths.

The ``atlassian_mcp_bitbucket/`` exclusion is critical: that gateway
already ships a vendored ``helm/`` chart of its own. Walking into it
would produce false positives for the ``**/helm`` pattern even though
the platform has not produced any new helm artifacts.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

import pytest

# ``conftest.py`` lives one directory up; pytest auto-loads it, but we
# add ``tests/`` to ``sys.path`` defensively so this file works when
# invoked directly via ``python -m pytest tests/property``.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import FORBIDDEN_PATHS, WORKSPACE_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Walk-time pruning
# ---------------------------------------------------------------------------

#: Directories that are pruned during the recursive walk used to evaluate
#: glob patterns. The set contains:
#:
#: - Vendored / build-artifact trees that would slow the walk and may
#:   contain unrelated ``helm/`` charts (e.g. ``node_modules/``,
#:   ``.venv/``, ``.next/``, ``dist/``).
#: - Tooling and cache trees (``.git/``, ``.pytest_cache/``,
#:   ``.hypothesis/``, ``__pycache__/``).
#: - ``atlassian_mcp_bitbucket/`` - gateway subtree which legitimately
#:   owns its own ``helm/`` chart.
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
    }
)


def _is_glob_pattern(path: str) -> bool:
    """Return True if ``path`` contains a recursive glob segment (``**``)."""

    return "**" in path


def _glob_basename(pattern: str) -> str:
    """Return the trailing fnmatch component of a ``**/<basename>`` pattern.

    The patterns in ``FORBIDDEN_PATHS`` are all of the form
    ``**/<basename-glob>`` (e.g. ``**/helm``, ``**/*ScaledObject*.yaml``,
    ``**/keda-*.yaml``). Stripping the leading ``**/`` yields the
    basename glob that ``fnmatch`` can evaluate against the leaf name
    of every walked path.
    """

    # Use POSIX-style splitting; ``FORBIDDEN_PATHS`` entries always use
    # forward slashes regardless of the host OS.
    head, sep, tail = pattern.rpartition("/")
    if not sep:
        return pattern
    return tail


def _walk_workspace(root: Path) -> list[Path]:
    """Walk ``root`` yielding every file and directory path, pruned.

    Heavy / vendored directories listed in ``_EXCLUDED_DIR_NAMES`` are
    pruned in-place so the walk stays scoped to the project's own
    output. Returned paths are absolute ``Path`` instances; callers
    typically only inspect the leaf name.
    """

    found: list[Path] = []
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        # Prune in-place so ``os.walk`` does not descend into excluded
        # subtrees. This must mutate the existing list, not rebind it.
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        base = Path(dirpath)
        for d in dirnames:
            found.append(base / d)
        for f in filenames:
            found.append(base / f)
    return found


# Compute the pruned workspace inventory once at module import. The walk
# is deterministic and a single workspace traversal is cheap enough to
# share across every parametrised case.
_WORKSPACE_INVENTORY: list[Path] = _walk_workspace(WORKSPACE_ROOT)


# ---------------------------------------------------------------------------
# Out-of-scope artifact absence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden", FORBIDDEN_PATHS, ids=list(FORBIDDEN_PATHS))
def test_out_of_scope_artifacts_absent(forbidden: str) -> None:
    """Every entry in ``FORBIDDEN_PATHS`` is absent.

    Two cases per entry:

    * Literal path (no ``**``): assert ``WORKSPACE_ROOT / forbidden``
      does not exist as a file or directory.
    * Glob pattern (contains ``**``): walk the workspace tree (with
      heavy directories pruned) and assert no path's leaf name matches
      the pattern's trailing fnmatch component.
    """

    if not _is_glob_pattern(forbidden):
        candidate = WORKSPACE_ROOT / forbidden
        assert not candidate.exists(), (
            f"Out-of-scope artifact present at workspace root: "
            f"{candidate} (forbidden literal path '{forbidden}'). "
            f"The project must not produce "
            f"this directory or file."
        )
        return

    basename_pattern = _glob_basename(forbidden)
    matches: list[Path] = [
        path
        for path in _WORKSPACE_INVENTORY
        if fnmatch.fnmatch(path.name, basename_pattern)
    ]

    assert not matches, (
        f"Out-of-scope artifact(s) match forbidden glob '{forbidden}' "
        f"(basename pattern '{basename_pattern}'): "
        f"{[str(p.relative_to(WORKSPACE_ROOT)) for p in matches]}. "
        f"Helm/k8s/KEDA artifacts MUST NOT appear anywhere outside the "
        f"atlassian_mcp_bitbucket/ gateway tree."
    )
