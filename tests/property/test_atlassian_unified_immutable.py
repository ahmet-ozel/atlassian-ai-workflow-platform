"""Property test for ``atlassian_unified/`` content immutability.

**Validates: Requirements 2.4, 9.8** (spec ``platform-mimari-foundation``).

Property 14 — ``atlassian_unified/`` immutability.

The ``atlassian_unified/`` directory is an existing, immutable input to
the platform. ``platform-mimari-foundation`` Requirement 2.4 forbids
modifying, adding, or removing any file under ``services/atlassian_unified/``
and Requirement 9.8 lists ``atlassian_unified immutability`` as a
property-test-enforced invariant: the directory's per-file SHA-256 hash
list must match the committed reference manifest exactly (MIMARI §1
Kural 1, §17).

Historical note: an earlier ``multi-service-scaffold`` spec covered the
same invariant under Requirements 1.8 / 17.1; this test originates from
that spec and is reused here verbatim because the underlying invariant
is identical (file-level immutability of ``services/atlassian_unified/``).

The property guards the invariant by:

1. Walking ``atlassian_unified/`` once at session scope (excluding
   transient build / cache directories that are not part of the source
   tree) and computing a ``(relative_path, sha256)`` mapping.
2. Persisting that mapping at
   ``tests/fixtures/atlassian_unified_baseline.json`` on the first run
   (bootstrap), and comparing every subsequent run against the
   committed baseline.

Two complementary checks run on every invocation:

* A Hypothesis property samples ``(path, expected_sha256)`` pairs from
  the baseline and asserts each file still exists and still hashes to
  the recorded digest. Hypothesis shrinks to the single offending file
  when a regression occurs, which is the actionable signal.
* A parametric test asserts the **exact** set equality between the
  current snapshot's path set and the baseline's path set, surfacing
  additions and removals that a sampled property could miss.

Together these enforce ``current_snapshot == baseline`` in both
directions: no file may be added, removed, or modified relative to the
baseline.

Bootstrap behaviour
-------------------
If ``tests/fixtures/atlassian_unified_baseline.json`` does not exist
when the test session starts, the session fixture computes the current
snapshot and writes it as the baseline. The subsequent assertions pass
trivially (current == baseline by construction). The new baseline file
is the artefact the test author commits so future runs become true
regression checks. A clear log line is emitted to make the bootstrap
visible.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Mapping

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module via ``rootdir``. We append ``tests/`` to ``sys.path``
# defensively so the module also imports cleanly under direct
# ``python -m pytest tests/property/...`` invocations (mirrors the
# pattern used by other property tests in this directory).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import WORKSPACE_ROOT  # noqa: E402


# ---------------------------------------------------------------------------
# Constants — what to walk, what to skip, where the baseline lives
# ---------------------------------------------------------------------------

#: Path under the workspace root that this property guards.
ATLASSIAN_UNIFIED_ROOT: Path = WORKSPACE_ROOT / "services" / "atlassian_unified"

#: Where the committed baseline manifest is persisted.
BASELINE_PATH: Path = (
    WORKSPACE_ROOT / "tests" / "fixtures" / "atlassian_unified_baseline.json"
)

#: Directory names that are transient build / cache / VCS metadata and
#: therefore excluded from the immutability snapshot. Anything inside
#: ``atlassian_unified/`` that develops or regenerates these directories
#: locally must not break the invariant.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".env",  # accidental local virtualenvs / dotenv state
        "__pycache__",
        ".pytest_cache",
        ".hypothesis",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".coverage",
        ".idea",
        ".vscode",
        "node_modules",
        "dist",
        "build",
        ".next",
        ".turbo",
        ".cache",
        "htmlcov",
        ".eggs",
        ".dvc",
    }
)

#: File suffixes that are transient compilation / cache artefacts. These
#: never belong in the baseline even if they happen to live outside an
#: excluded directory.
_EXCLUDED_FILE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
    }
)

#: Chunk size for streaming SHA-256 — large enough to keep IO efficient
#: on the ~500-file walk, small enough not to balloon memory.
_HASH_CHUNK_SIZE: int = 65_536


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _is_excluded_dir(directory: Path) -> bool:
    """Return ``True`` when *any* path component is an excluded dir.

    We test the entire relative-to-``atlassian_unified`` path so a deeply
    nested ``foo/bar/__pycache__/baz.pyc`` is filtered even when the
    iteration enters ``__pycache__`` indirectly via a recursive walk.
    """

    for part in directory.parts:
        if part in _EXCLUDED_DIR_NAMES:
            return True
    return False


def _hash_file(path: Path) -> str:
    """Streaming SHA-256 hex digest for a single file.

    Uses a fixed chunk size to bound peak memory regardless of file
    size. Binary mode reads are mandatory to keep digests stable across
    operating systems (text-mode normalisation would corrupt the hash).
    """

    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_snapshot(root: Path) -> dict[str, str]:
    """Walk ``root`` and return ``{relative_posix_path: sha256_hex}``.

    Paths are stored in POSIX form (forward slashes) so the JSON
    baseline is portable across operating systems. Excluded directories
    (per :data:`_EXCLUDED_DIR_NAMES`) and excluded suffixes (per
    :data:`_EXCLUDED_FILE_SUFFIXES`) are skipped.
    """

    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot

    for entry in root.rglob("*"):
        # Skip non-files quickly. Symlinks to directories are not
        # followed here because ``rglob`` already enumerates real
        # filesystem entries; symlinks to files are hashed as files.
        if not entry.is_file():
            continue

        relative = entry.relative_to(root)

        # Filter excluded directories anywhere on the path.
        if _is_excluded_dir(relative.parent):
            continue

        # Filter excluded filename suffixes.
        if entry.suffix.lower() in _EXCLUDED_FILE_SUFFIXES:
            continue

        # Skip the relative path if any of its components is itself an
        # excluded dir name (catches symlink-style edge cases).
        if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts):
            continue

        snapshot[relative.as_posix()] = _hash_file(entry)

    return snapshot


def _load_or_bootstrap_baseline(snapshot: Mapping[str, str]) -> dict[str, str]:
    """Load the committed baseline; if missing, write *snapshot* as the new one.

    The bootstrap path keeps first-run ergonomics simple: the test
    suite is self-priming. Subsequent runs read the committed baseline
    and perform a strict comparison.
    """

    if BASELINE_PATH.exists():
        with BASELINE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise TypeError(
                f"Baseline {BASELINE_PATH} is malformed: expected a JSON "
                f"object mapping path -> sha256, got {type(data).__name__}."
            )
        # Defensive copy to make returned mapping mutation-safe.
        return dict(data)

    # Bootstrap: create the fixtures dir if needed and persist the
    # current snapshot as the new baseline. ``sort_keys=True`` keeps
    # diffs stable; a trailing newline keeps the file POSIX-friendly.
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BASELINE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(dict(sorted(snapshot.items())), handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(
        f"[atlassian_unified_immutable] bootstrap: wrote baseline with "
        f"{len(snapshot)} entries to {BASELINE_PATH.relative_to(WORKSPACE_ROOT)}"
    )
    return dict(snapshot)


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def atlassian_unified_snapshot() -> dict[str, str]:
    """Current ``(path -> sha256)`` snapshot of ``atlassian_unified/``.

    Computed once per test session; downstream tests reuse the same
    mapping rather than re-walking the directory.
    """

    if not ATLASSIAN_UNIFIED_ROOT.is_dir():
        pytest.skip(
            "services/atlassian_unified/ is not present at the workspace root; "
            "Property 14 has nothing to enforce."
        )
    return _compute_snapshot(ATLASSIAN_UNIFIED_ROOT)


@pytest.fixture(scope="session")
def atlassian_unified_baseline(
    atlassian_unified_snapshot: dict[str, str],
) -> dict[str, str]:
    """Committed baseline manifest, bootstrapped on first run.

    Reads ``tests/fixtures/atlassian_unified_baseline.json`` if it
    exists; otherwise persists *atlassian_unified_snapshot* as the new
    baseline so the first run is non-destructive.
    """

    return _load_or_bootstrap_baseline(atlassian_unified_snapshot)


# ---------------------------------------------------------------------------
# Property test — sampled hash equality
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(data=st.data())
def test_baseline_entry_still_matches_on_disk(
    data: st.DataObject,
    atlassian_unified_snapshot: dict[str, str],
    atlassian_unified_baseline: dict[str, str],
) -> None:
    """Property 14a — every sampled baseline entry still matches on disk.

    Strategy: ``st.sampled_from(baseline.items())`` pulls a single
    ``(relative_path, expected_sha256)`` pair. The assertion verifies
    the path still exists in the current snapshot AND its current hash
    equals the baseline hash. Hypothesis shrinks to the single offending
    pair when the property fails.

    The empty-baseline edge case (``atlassian_unified/`` legitimately
    contains zero tracked files) is handled by skipping rather than by
    a vacuous pass — a vacuous pass would silently mask a misconfigured
    walk.
    """

    if not atlassian_unified_baseline:
        pytest.skip(
            "Baseline is empty; nothing to sample. This indicates either a "
            "first-bootstrap run on an empty atlassian_unified/ or a walk "
            "misconfiguration."
        )

    baseline_items = sorted(atlassian_unified_baseline.items())
    relative_path, expected_hash = data.draw(
        st.sampled_from(baseline_items),
        label="baseline_entry",
    )

    current_hash = atlassian_unified_snapshot.get(relative_path)
    assert current_hash is not None, (
        f"atlassian_unified/ file recorded in baseline is missing on disk: "
        f"{relative_path!r}. Property 14 forbids deleting baseline files."
    )
    assert current_hash == expected_hash, (
        f"atlassian_unified/{relative_path} has been modified.\n"
        f"  baseline sha256: {expected_hash}\n"
        f"  current  sha256: {current_hash}\n"
        f"Property 14 (Requirements 2.4, 9.8) forbids modifying files "
        f"under atlassian_unified/."
    )


# ---------------------------------------------------------------------------
# Path-set equality — catches additions and removals
# ---------------------------------------------------------------------------


def test_snapshot_path_set_equals_baseline(
    atlassian_unified_snapshot: dict[str, str],
    atlassian_unified_baseline: dict[str, str],
) -> None:
    """Property 14b — current path set equals baseline path set.

    Sampling alone cannot detect *additions* (Hypothesis only sees the
    baseline universe). A direct set-equality assertion closes that
    gap: any file that appears in the current snapshot but is absent
    from the baseline, or vice-versa, is reported with the full diff.
    """

    current_paths = set(atlassian_unified_snapshot.keys())
    baseline_paths = set(atlassian_unified_baseline.keys())

    added = sorted(current_paths - baseline_paths)
    removed = sorted(baseline_paths - current_paths)

    diagnostic_lines: list[str] = []
    if added:
        diagnostic_lines.append(
            f"Added {len(added)} unexpected file(s) under atlassian_unified/:"
        )
        diagnostic_lines.extend(f"  + {p}" for p in added[:20])
        if len(added) > 20:
            diagnostic_lines.append(f"  ... and {len(added) - 20} more")
    if removed:
        diagnostic_lines.append(
            f"Removed {len(removed)} baseline file(s) under atlassian_unified/:"
        )
        diagnostic_lines.extend(f"  - {p}" for p in removed[:20])
        if len(removed) > 20:
            diagnostic_lines.append(f"  ... and {len(removed) - 20} more")

    assert current_paths == baseline_paths, (
        "Property 14 (Requirements 2.4, 9.8) violated: "
        "atlassian_unified/ path set drifted from baseline.\n"
        + "\n".join(diagnostic_lines)
    )


# ---------------------------------------------------------------------------
# Bulk hash check — fast O(n) verification independent of Hypothesis
# ---------------------------------------------------------------------------


def test_every_baseline_entry_matches_current(
    atlassian_unified_snapshot: dict[str, str],
    atlassian_unified_baseline: dict[str, str],
) -> None:
    """Property 14c — exhaustive ``baseline ⊆ current`` hash equality.

    Complements the sampled property by deterministically verifying
    every baseline entry. Hypothesis sampling is sufficient to *find*
    drift, but a parametric exhaustive check here means CI fails with
    the **complete** list of regressions rather than the first one
    Hypothesis happened to draw.
    """

    mismatches: list[str] = []
    for relative_path, expected_hash in sorted(atlassian_unified_baseline.items()):
        current_hash = atlassian_unified_snapshot.get(relative_path)
        if current_hash is None:
            mismatches.append(f"  - {relative_path} (deleted)")
            continue
        if current_hash != expected_hash:
            mismatches.append(
                f"  ~ {relative_path} (baseline={expected_hash[:12]}…, "
                f"current={current_hash[:12]}…)"
            )

    assert not mismatches, (
        "Property 14 (Requirements 2.4, 9.8) violated: "
        f"{len(mismatches)} baseline file(s) modified or removed:\n"
        + "\n".join(mismatches[:50])
        + ("\n  …" if len(mismatches) > 50 else "")
    )
