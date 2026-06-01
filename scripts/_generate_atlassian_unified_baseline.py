"""Generate the ``atlassian_unified`` baseline hash manifest.

This script mirrors the snapshot logic embedded in
``tests/property/test_atlassian_unified_immutable.py`` so the committed
baseline JSON is produced with identical semantics:

* Same walk root (``services/atlassian_unified``).
* Same excluded directory names and file suffixes.
* Same SHA-256 streaming implementation.
* Same POSIX-style relative paths, sorted lexicographically.
* Same on-disk format (``indent=2``, ``sort_keys=True``, trailing newline).

Running this script idempotently regenerates the file at
``platform/tests/fixtures/atlassian_unified_baseline.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Resolve the platform/ root from this file's location:
#   platform/scripts/_generate_atlassian_unified_baseline.py
PLATFORM_ROOT: Path = Path(__file__).resolve().parent.parent

ATLASSIAN_UNIFIED_ROOT: Path = PLATFORM_ROOT / "services" / "atlassian_unified"
BASELINE_PATH: Path = (
    PLATFORM_ROOT / "tests" / "fixtures" / "atlassian_unified_baseline.json"
)

_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".env",
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

_EXCLUDED_FILE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo", ".pyd"})

_HASH_CHUNK_SIZE: int = 65_536


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot

    for entry in root.rglob("*"):
        if not entry.is_file():
            continue

        relative = entry.relative_to(root)

        if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts):
            continue

        if entry.suffix.lower() in _EXCLUDED_FILE_SUFFIXES:
            continue

        snapshot[relative.as_posix()] = _hash_file(entry)

    return snapshot


def main() -> int:
    if not ATLASSIAN_UNIFIED_ROOT.is_dir():
        print(f"ERROR: {ATLASSIAN_UNIFIED_ROOT} does not exist.")
        return 1

    snapshot = _compute_snapshot(ATLASSIAN_UNIFIED_ROOT)
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)

    sorted_snapshot = dict(sorted(snapshot.items()))
    with BASELINE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(sorted_snapshot, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(
        f"Wrote {len(sorted_snapshot)} entries to "
        f"{BASELINE_PATH.relative_to(PLATFORM_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
