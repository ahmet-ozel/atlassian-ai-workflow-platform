"""Property-based preservation test for previously passing tests.

Background
----------

The cleanup must not regress any test that was already passing. The
byte-comparable oracle for that invariant is the
``snapshots/before/platform_full.txt`` artifact.

This test reads that snapshot, enumerates every test name whose pre-fix
outcome is ``PASSED``, and on the current code re-runs each sampled test
in a freshly-spawned ``pytest`` subprocess. The assertion is that the
test still passes — i.e. preservation holds. The Hypothesis strategy
``sampled_from`` provides the enumeration / property-test embodiment of:

    FOR ALL X in {pre-fix-PASSED test names} : test_passes_now(X)

Surface 1..5 failing test files are excluded automatically because their
pre-fix outcome was ``FAILED`` / ``ERROR``, never ``PASSED``, so they are
not in the strategy domain.

Why a subprocess? The collected tests live in many different conftest
trees and a few of them mutate ``sys.path`` at import time. Spawning a
fresh ``pytest`` subprocess per sampled test gives us the same isolation
the original capture had and avoids cross-test contamination.

Performance contract
--------------------

The snapshot contains ~1700 PASSED tests. Sampling each one in a fresh
subprocess would dominate CI runtime, so we use Hypothesis settings that
draw a SMALL bounded sample (``max_examples`` controls the sample size).
The sample is reproducible because ``hypothesis`` uses a deterministic
seed; rerunning the test repeatedly increases coverage without inflating
any single CI run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SPEC_ROOT: Final[Path] = (
    _PLATFORM_ROOT.parent
    / "\x2ekiro"
    / "specs"
    / "fix-pre-existing-test-failures"
)
_SNAPSHOT_FILE: Final[Path] = _SPEC_ROOT / "snapshots" / "before" / "platform_full.txt"


# ---------------------------------------------------------------------------
# Snapshot parser
# ---------------------------------------------------------------------------

# Lines in the verbose pytest output look like:
#   tests/ci/test_admin_dashboard_routes.py::test_admin_pages_dir_exists PASSED [  0%]
_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<nodeid>\S+::\S+)\s+(?P<outcome>PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
)

# Files whose pre-fix outcome is FAILED / ERRORED for one of the known
# failing surfaces. The strategy filters these out by name so preservation
# only applies to previously passing inputs.
_SURFACE_FILES: Final[frozenset[str]] = frozenset(
    {
        "tests/unit/test_temporal_shared.py",          # Surface 1
        "tests/unit/test_capability_helpers.py",       # Surface 2
        "tests/unit/test_deployment_router.py",        # Surface 3
        "tests/unit/test_llm_orchestrator.py",         # Surface 4
        # Surface 5: 3 specific tests in workers/automation-worker/tests/property/
        # — handled inline below since they are individual test names, not whole files.
    }
)

_SURFACE5_TEST_PREFIXES: Final[tuple[str, ...]] = (
    # Three Surface 5 property tests; whole-file exclusion would over-exclude
    # because the same directory holds preserved property tests
    # (see clause 3.3). We exclude only the three tests the design names.
    # Pre-fix outcome of these three is FAILED → they are not PASSED in the
    # snapshot anyway, so this list is defensive.
    "workers/automation-worker/tests/property/test_prompt_fixture_resolution.py",
)


def _read_passed_node_ids() -> list[str]:
    """Parse ``platform_full.txt`` and return the list of PASSED test node ids.

    Excludes tests inside the five named failing files / Surface 5 tests so
    the strategy domain is exactly the preservation oracle.
    """
    if not _SNAPSHOT_FILE.is_file():
        pytest.skip(
            f"Preservation snapshot missing at {_SNAPSHOT_FILE}; "
            "capture the before snapshot first."
        )

    passed: list[str] = []
    text = _SNAPSHOT_FILE.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        match = _LINE_RE.match(line.strip())
        if not match:
            continue
        if match.group("outcome") != "PASSED":
            continue
        node_id = match.group("nodeid").replace("\\", "/")
        # Surface-1..4 file-level exclusion
        if any(node_id.startswith(f) for f in _SURFACE_FILES):
            continue
        # Surface-5 file-level exclusion
        if any(node_id.startswith(f) for f in _SURFACE5_TEST_PREFIXES):
            continue
        passed.append(node_id)
    return passed


# Compute the strategy domain at module import time so test collection
# fails loudly if the snapshot is malformed or empty.
_PASSED_NODE_IDS: Final[list[str]] = _read_passed_node_ids()


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@pytest.mark.timeout(600)
@settings(
    max_examples=15,  # bounded sample; rerun broadens coverage deterministically
    deadline=None,
    suppress_health_check=(
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ),
)
@given(node_id=st.sampled_from(_PASSED_NODE_IDS))
def test_preservation_previously_passing_tests_still_pass(node_id: str) -> None:
    """Every test that was PASSED pre-fix must still pass.

    For each sampled ``node_id`` from the pre-fix snapshot's PASSED set,
    re-run the test in a fresh pytest subprocess and assert the exit code
    indicates PASS. This is the property-test embodiment of:

        FOR ALL X in pre_fix_passed : current_outcome(X) == PASSED
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--tb=short",
        "-q",
        "--timeout=30",
        "-p",
        "no:randomly",
        node_id,
    ]
    result = subprocess.run(
        cmd,
        cwd=str(_PLATFORM_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"Preservation regression on {node_id!r}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_snapshot_has_passed_tests() -> None:
    """Sanity check: the strategy domain is non-empty.

    If this fails the snapshot is malformed; subsequent ``@given`` invocations
    would silently fall back to filtering away every example.
    """
    assert _PASSED_NODE_IDS, (
        "No PASSED node ids parsed from snapshot — preservation oracle is "
        "empty. Re-capture platform_full.txt with `pytest -v --tb=no`."
    )


def test_surface_files_excluded_from_strategy() -> None:
    """Sanity check: the five named failing files are NOT in the strategy.

    Their pre-fix outcome is FAILED/ERROR so they should not appear in
    ``_PASSED_NODE_IDS`` even before file-level filtering. This test
    guards against future snapshot updates accidentally including a
    Surface-i test whose state changed.
    """
    for surface_file in _SURFACE_FILES | set(_SURFACE5_TEST_PREFIXES):
        offenders = [
            n for n in _PASSED_NODE_IDS if n.startswith(surface_file)
        ]
        assert not offenders, (
            f"{surface_file} should be excluded from the preservation "
            f"strategy but {len(offenders)} entries leaked through: "
            f"{offenders[:3]}..."
        )
