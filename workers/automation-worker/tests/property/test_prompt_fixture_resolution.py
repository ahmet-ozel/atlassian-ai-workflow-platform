"""Invariant test: Surface 5 bug-condition exploration - fixture path resolution.

**Bug Condition (Surface 5)**: The ``DEFAULT_PROMPT_PATH`` in
``automation_worker.activities.task_analyzer`` is resolved via
``Path(__file__).resolve.parents[5] / "prompts" / "task_analysis.md"``.
This resolves to ``platform/prompts/task_analysis.md`` which does NOT exist.
The canonical prompt lives at
``platform/workers/agent-runner-worker/prompts/task_analysis.md``.

**Failing CWDs and resolved paths (documented from exploration run)**:

When pytest is invoked from the workspace root
(``c:/Users/ahmet/Desktop/atlassian-ai-workflow-platform``):
 - ``DEFAULT_PROMPT_PATH`` resolves to:
 ``C:\\Users\\ahmet\\Desktop\\atlassian-ai-workflow-platform\\platform\\prompts\\task_analysis.md``
 - ``is_file`` → False ← BUG CONDITION

When pytest is invoked from ``platform/``:
 - Same absolute resolution (``__file__``-anchored, not CWD-relative):
 ``C:\\Users\\ahmet\\Desktop\\atlassian-ai-workflow-platform\\platform\\prompts\\task_analysis.md``
 - ``is_file`` → False ← BUG CONDITION

When pytest is invoked from ``platform/workers/automation-worker/``:
 - Same absolute resolution:
 ``C:\\Users\\ahmet\\Desktop\\atlassian-ai-workflow-platform\\platform\\prompts\\task_analysis.md``
 - ``is_file`` → False ← BUG CONDITION

When pytest is invoked from ``platform/workers/agent-runner-worker/``:
 - Same absolute resolution:
 ``C:\\Users\\ahmet\\Desktop\\atlassian-ai-workflow-platform\\platform\\prompts\\task_analysis.md``
 - ``is_file`` → False ← BUG CONDITION

Canonical prompt (always exists):
 ``platform/workers/agent-runner-worker/prompts/task_analysis.md``
 ``is_file`` → True

**isBugCondition_5(X)**:
 ``Path(resolved).is_file == False AND canonical_path.is_file == True``

**Expected outcome on UNFIXED code**: This test FAILS with AssertionError
because ``DEFAULT_PROMPT_PATH.is_file`` is False for every simulated CWD -
the path is ``__file__``-anchored but points to the wrong location
(``platform/prompts/`` instead of
``platform/workers/agent-runner-worker/prompts/``).

**Expected outcome on FIXED code**: This test PASSES because the fixed
``DEFAULT_PROMPT_PATH`` uses the correct ``parents[N]`` offset to reach
``platform/workers/agent-runner-worker/prompts/task_analysis.md``.

**Preservation clause 3.7**: This test ONLY reads the canonical prompt for
its content hash. It does NOT modify, move, or copy the file.

**"""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors the pattern used by sibling Invariant tests)
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities.task_analyzer import DEFAULT_PROMPT_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Canonical prompt - MUST NOT be modified (preservation clause 3.7)
# ---------------------------------------------------------------------------

# The canonical prompt lives at:
# platform/workers/agent-runner-worker/prompts/task_analysis.md
# We locate it relative to this test file:
# __file__ = platform/workers/automation-worker/tests/property/<this file>
# parents[0] = platform/workers/automation-worker/tests/property/
# parents[1] = platform/workers/automation-worker/tests/
# parents[2] = platform/workers/automation-worker/
# parents[3] = platform/workers/
# parents[4] = platform/
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[4]
_CANONICAL_PROMPT: Path = (
    _PLATFORM_ROOT
    / "workers"
    / "agent-runner-worker"
    / "prompts"
    / "task_analysis.md"
)

# ---------------------------------------------------------------------------
# Plausible CWDs to simulate (task spec)
# ---------------------------------------------------------------------------

# These are the four CWDs the spec requires us to enumerate:
# 1. workspace_root (parent of platform/)
# 2. platform/
# 3. platform/workers/automation-worker/
# 4. an unrelated nested directory under platform/
_WORKSPACE_ROOT: Path = _PLATFORM_ROOT.parent
_AUTOMATION_WORKER_DIR: Path = _PLATFORM_ROOT / "workers" / "automation-worker"
_UNRELATED_NESTED_DIR: Path = _PLATFORM_ROOT / "workers" / "agent-runner-worker"

_PLAUSIBLE_CWDS: list[Path] = [
    _WORKSPACE_ROOT,
    _PLATFORM_ROOT,
    _AUTOMATION_WORKER_DIR,
    _UNRELATED_NESTED_DIR,
]

# ---------------------------------------------------------------------------
# Content hash of the canonical prompt (read once at module import time)
# Preservation clause 3.7: we only READ the file, never write/move it.
# ---------------------------------------------------------------------------

_CANONICAL_CONTENT_HASH: str | None = None
if _CANONICAL_PROMPT.is_file():
    _CANONICAL_CONTENT_HASH = hashlib.sha256(
        _CANONICAL_PROMPT.read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------------------
# CWD context manager (avoids monkeypatch with Hypothesis)
# ---------------------------------------------------------------------------


@contextmanager
def _chdir(path: Path) -> Generator[None, None, None]:
    """Temporarily change the process CWD; restore it on exit."""
    original = Path.cwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(original)


# ---------------------------------------------------------------------------
# Invariant test
# ---------------------------------------------------------------------------


class TestSurface5PromptFixtureResolution:
    """Surface 5 bug-condition exploration test.

 **: Bug Condition** - invariant Fixtures Resolve Canonical Prompt

 For each plausible CWD, assert that ``DEFAULT_PROMPT_PATH`` resolves to
 an existing file whose content matches the canonical prompt.

 On UNFIXED code this test FAILS because ``DEFAULT_PROMPT_PATH`` points
 to ``platform/prompts/task_analysis.md`` (non-existent).

 **"""

    @given(
        cwd=st.sampled_from(_PLAUSIBLE_CWDS),
    )
    @settings(
        max_examples=4,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_surface5_prompt_fixture_resolves_from_any_cwd(
        self,
        cwd: Path,
    ) -> None:
        """For every plausible CWD, DEFAULT_PROMPT_PATH must resolve to the
 canonical prompt file.

 **isBugCondition_5(X)**:
 ``DEFAULT_PROMPT_PATH.is_file == False``
 AND ``_CANONICAL_PROMPT.is_file == True``

 On UNFIXED code: FAILS (DEFAULT_PROMPT_PATH does not exist).
 On FIXED code: PASSES (DEFAULT_PROMPT_PATH resolves to canonical).

 **"""
        # Simulate the CWD using a context manager so the process CWD
        # is restored after each generated example.
        with _chdir(cwd):
            # --- Canonical-existence half (should always pass) ---
            # The canonical prompt MUST exist regardless of CWD.
            assert _CANONICAL_PROMPT.is_file(), (
                f"Canonical prompt not found at {_CANONICAL_PROMPT!r}. "
                "This file must not be moved (preservation clause 3.7)."
            )

            # --- Bug-condition check ---
            # On unfixed code: DEFAULT_PROMPT_PATH.is_file == False
            # This assertion encodes isBugCondition_5 and is expected to FAIL
            # on unfixed code, confirming Surface 5 exists.
            resolved = DEFAULT_PROMPT_PATH
            assert resolved.is_file(), (
                f"BUG CONDITION DETECTED (Surface 5): "
                f"DEFAULT_PROMPT_PATH={resolved!r} does not exist "
                f"when CWD={cwd!r}. "
                f"Canonical prompt at {_CANONICAL_PROMPT!r} DOES exist. "
                f"isBugCondition_5 is TRUE: the fixture path is wrong."
            )

            # --- Content-hash check (only reached on fixed code) ---
            # Verify the resolved path contains the same content as the canonical.
            assert _CANONICAL_CONTENT_HASH is not None, (
                "Could not compute canonical content hash - "
                f"canonical prompt not found at {_CANONICAL_PROMPT!r}"
            )
            resolved_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
            assert resolved_hash == _CANONICAL_CONTENT_HASH, (
                f"DEFAULT_PROMPT_PATH content does not match canonical prompt. "
                f"resolved={resolved!r}, canonical={_CANONICAL_PROMPT!r}"
            )
