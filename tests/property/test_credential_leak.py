"""invariant for credential leak invariant.



invariant: Credential leak invariant

For any path matching sensitive credential file patterns (`.env`,
`services/foo/.env`, `apps/bar/.env`, `credentials.md`), the repository's
`.gitignore` MUST transitively ignore that path (verified via
`git check-ignore --no-index`), and `git status --porcelain` MUST NOT
report any tracked or staged file matching these patterns.

This ensures that no credential file can accidentally be committed and
pushed to a remote, preventing token/key/password leakage.

Strategy
--------
Hypothesis generates random paths matching `.env`-like patterns:
- Root `.env`
- Nested `.env` files under arbitrary directory prefixes
 (e.g. `services/foo/.env`, `apps/bar/.env`)
- `credentials.md` at root and nested locations

For each generated path, the test asserts:
1. `git check-ignore --no-index <path>` exits with code 0 (path is
 covered by `.gitignore` rules)
2. `git status --porcelain` output contains no lines matching the
 sensitive patterns
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hypothesis import HealthCheck, assume, given, note
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Workspace root resolution
# ---------------------------------------------------------------------------

#: Workspace root: ``tests/property/X`` -> ``tests/property`` -> ``tests``
#: -> ``<platform>``
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_WORKSPACE_ROOT: Path = _PLATFORM_ROOT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_git_root() -> Path | None:
    """Find the git repository root, or None if not in a git repo.

 Checks both the workspace root and the platform root for a.git
 directory or file (submodule case).
 """
    for candidate in (_WORKSPACE_ROOT, _PLATFORM_ROOT):
        git_dir = candidate / ".git"
        if git_dir.exists():
            return candidate
    # Fallback: try git rev-parse
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=str(_WORKSPACE_ROOT),
            timeout=10,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _has_gitignore() -> bool:
    """Check if a.gitignore file exists at the workspace root."""
    return (_WORKSPACE_ROOT / ".gitignore").is_file()


def _git_check_ignore(path: str, cwd: Path) -> bool:
    """Return True if the given path is ignored by git (exit code 0).

 Uses `git check-ignore --no-index` which checks.gitignore rules
 without requiring the path to exist on disk and without needing
 a full git index.
 """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", path],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _git_status_porcelain(cwd: Path) -> str:
    """Run `git status --porcelain` and return stdout.

 Returns empty string if git is not available or not in a repo.
 """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _gitignore_contains_pattern(pattern: str) -> bool:
    """Check if the workspace.gitignore contains the given literal pattern."""
    gitignore_path = _WORKSPACE_ROOT / ".gitignore"
    if not gitignore_path.is_file():
        return False
    content = gitignore_path.read_text(encoding="utf-8")
    # Check for the pattern as a standalone line (stripped)
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == pattern:
            return True
    return False


def _is_git_repo(cwd: Path) -> bool:
    """Check if the given directory is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Directory name segments for generating nested.env paths
_dir_segment = st.from_regex(r"\A[a-z][a-z0-9_-]{0,15}\Z", fullmatch=True)

#: Strategy for generating.env-pattern-matching paths
_env_paths = st.one_of(
    # Root.env
    st.just(".env"),
    # Single-level nested: <dir>/.env
    _dir_segment.map(lambda d: f"{d}/.env"),
    # services/<name>/.env pattern
    _dir_segment.map(lambda d: f"services/{d}/.env"),
    # apps/<name>/.env pattern
    _dir_segment.map(lambda d: f"apps/{d}/.env"),
    # Deeper nesting: <dir>/<subdir>/.env
    st.tuples(_dir_segment, _dir_segment).map(
        lambda t: f"{t[0]}/{t[1]}/.env"
    ),
    # credentials.md at root
    st.just("credentials.md"),
    # Nested credentials.md (less common but should still be ignored)
    _dir_segment.map(lambda d: f"{d}/credentials.md"),
)


# ---------------------------------------------------------------------------
# invariant: Credential leak invariant — gitignore coverage
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(path=_env_paths)
def test_credential_paths_are_gitignored(path: str) -> None:
    """invariant — every.env-pattern path is covered by.gitignore.



 For any path matching sensitive credential file patterns, the
 repository's `.gitignore` rules MUST transitively ignore that path.
 This is verified via `git check-ignore --no-index <path>` which
 checks gitignore rules without requiring the file to exist on disk.

 If git is not initialized (no.git directory), the test falls back
 to verifying that the.gitignore file contains the required literal
 patterns (`.env`, `**/.env`, `credentials.md`).
 """
    note(f"Testing path: {path}")

    # Determine the working directory for git commands
    git_root = _find_git_root()

    if git_root is not None and _is_git_repo(git_root):
        # Full git check-ignore verification
        ignored = _git_check_ignore(path, cwd=git_root)
        assert ignored, (
            f"Path {path!r} is NOT ignored by.gitignore rules. "
            f"Running `git check-ignore --no-index {path}` in {git_root} "
            f"returned non-zero exit code. This path could be accidentally "
            f"committed, leaking credentials (the operational rule)."
        )
    else:
        # Fallback: verify.gitignore file contains required patterns
        assert _has_gitignore(), (
            "No.gitignore file found at workspace root. "
            "Credential files could be accidentally committed."
        )

        # Check that the essential patterns are present
        if path.endswith(".env"):
            #.env files should be covered by `.env` or `**/.env`
            has_env_pattern = (
                _gitignore_contains_pattern(".env")
                or _gitignore_contains_pattern("**/.env")
            )
            assert has_env_pattern, (
                f"Path {path!r} is an.env file but.gitignore does not "
                f"contain `.env` or `**/.env` pattern. Credential files "
                f"could be accidentally committed (the operational rule)."
            )
        elif "credentials.md" in path:
            assert _gitignore_contains_pattern("credentials.md"), (
                f"Path {path!r} matches credentials.md but.gitignore "
                f"does not contain `credentials.md` pattern. Credential "
                f"files could be accidentally committed (the operational rule)."
            )


# ---------------------------------------------------------------------------
# invariant: git status must not show credential files
# ---------------------------------------------------------------------------


# Sensitive file patterns for matching against git status output
_SENSITIVE_PATTERNS = (".env", "credentials.md")


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(data=st.data())
def test_git_status_has_no_credential_files(data: st.DataObject) -> None:
    """invariant — git status reports no credential file matches.



 The output of `git status --porcelain` in the repository root
 MUST NOT contain any line whose path matches `.env`, `*/.env`,
 `services/*/.env`, or `credentials.md`. This ensures no credential
 file is staged, modified, or untracked in the working tree.
 """
    git_root = _find_git_root()

    if git_root is None or not _is_git_repo(git_root):
        # If not in a git repo, verify.gitignore exists with patterns
        # (the gitignore file IS the protection mechanism pre-init)
        assert _has_gitignore(), (
            "No.gitignore file found and no git repo initialized. "
            "No protection against credential file commits exists."
        )
        return

    status_output = _git_status_porcelain(cwd=git_root)

    # Parse each line of git status output
    for line in status_output.splitlines():
        if not line.strip():
            continue
        # git status --porcelain format: XY <path> or XY <path> -> <path>
        # The path starts at position 3
        file_path = line[3:].strip()
        # Handle rename format: "old -> new"
        if " -> " in file_path:
            file_path = file_path.split(" -> ")[-1]

        # Check against sensitive patterns
        basename = os.path.basename(file_path)
        assert basename not in _SENSITIVE_PATTERNS, (
            f"git status reports a credential file in the working tree: "
            f"{file_path!r} (full line: {line!r}). This file should be "
            f"ignored by.gitignore and must never appear in git status "
            f"output (the operational rule)."
        )


# ---------------------------------------------------------------------------
# invariant:.gitignore required patterns presence
# ---------------------------------------------------------------------------


def test_gitignore_contains_required_credential_patterns() -> None:
    """invariant —.gitignore contains all required credential patterns.



 The workspace-root `.gitignore` MUST contain literal patterns for:
 - `.env` (root-level env file)
 - `**/.env` (all nested env files)
 - `credentials.md` (credential documentation)

 These patterns together ensure transitive coverage of all
 credential file locations.
 """
    assert _has_gitignore(), (
        "No.gitignore file found at workspace root "
        f"({_WORKSPACE_ROOT}). Cannot verify credential patterns."
    )

    required_patterns = [".env", "**/.env", "credentials.md"]
    missing = []

    for pattern in required_patterns:
        if not _gitignore_contains_pattern(pattern):
            missing.append(pattern)

    assert not missing, (
        f".gitignore is missing required credential patterns: {missing}. "
        f"These patterns are required to prevent accidental credential "
        f"commits (the operational rule). "
        f"File location: {_WORKSPACE_ROOT / '.gitignore'}"
    )
