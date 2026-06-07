"""Unit tests for the ``precommit_scanner`` activity.

Validates four scanner invariants:

1. **Clean diff  pass** - a diff with no secret patterns returns
   ``ScanResult(decision="pass", matched_patterns=())``.
2. **AWS access key  block** - an injected ``AKIA...`` literal is
   detected, returns ``decision="block"`` with ``"aws_access_key"``
   in ``matched_patterns``.
3. **Atlassian API token  block** - an injected ``ATATT3x...``
   literal is detected, returns ``decision="block"`` with
   ``"atlassian_api_token"`` in ``matched_patterns``.
4. **Determinism** - ``precommit_scanner(diff) == precommit_scanner(diff)``
   for any diff: the same input always yields the same
   :class:`ScanResult`.

The activity itself lives under
``platform/workers/agent-runner-worker/src/activities/precommit_scan.py``.
The Temporal worker package layout (``src.activities.*``) is not on
``pytest.ini``'s ``pythonpath``, so this module wires the worker
``src/`` directory onto :data:`sys.path` before importing - mirroring
the pattern used by the worker's own
``tests/unit/test_precommit_scan.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap - make ``src.activities.precommit_scan`` importable
# ---------------------------------------------------------------------------
#
# ``platform/pytest.ini`` only registers ``libs/*/src`` on ``pythonpath``;
# the worker source tree is intentionally not a shared lib. We inject
# the worker root (so ``src`` resolves as a package) ahead of import.

_TESTS_ROOT = Path(__file__).resolve().parent.parent  # platform/tests/
_PLATFORM_ROOT = _TESTS_ROOT.parent  # platform/
_WORKER_ROOT = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker"
)  # contains ``src`` as a sub-package

_worker_root_str = str(_WORKER_ROOT)
if _worker_root_str not in sys.path:
    sys.path.insert(0, _worker_root_str)

from src.activities.precommit_scan import (  # noqa: E402
    PRECOMMIT_AUDIT_ACTION,
    ScanResult,
    precommit_scanner,
    scan_diff,
)


# ---------------------------------------------------------------------------
# Test 1 - clean diff  pass
# ---------------------------------------------------------------------------


class TestCleanDiffPasses:
    """A diff without any documented secret pattern returns ``pass``."""

    def test_empty_diff_returns_pass(self) -> None:
        result = scan_diff("")
        assert result.decision == "pass"
        assert result.matched_patterns == ()

    def test_innocuous_python_diff_returns_pass(self) -> None:
        diff = (
            "--- a/src/util.py\n"
            "+++ b/src/util.py\n"
            "@@ -1,3 +1,3 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return a + b  # inline comment\n"
        )
        result = scan_diff(diff)
        assert result == ScanResult(decision="pass", matched_patterns=())

    def test_clean_diff_via_activity_entrypoint_passes(self) -> None:
        """The Temporal activity entrypoint also returns ``pass``."""
        result = asyncio.run(precommit_scanner("no secrets here, just prose"))
        assert result.decision == "pass"
        assert result.matched_patterns == ()


# ---------------------------------------------------------------------------
# Test 2 - injected AWS key  block + ``aws_access_key`` matched
# ---------------------------------------------------------------------------


class TestAwsAccessKeyIsBlocked:
    """An injected AWS access key literal triggers a block decision."""

    def test_aws_access_key_in_assignment_blocks(self) -> None:
        diff = 'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"'
        result = scan_diff(diff)
        assert result.decision == "block"
        assert "aws_access_key" in result.matched_patterns

    def test_aws_access_key_in_diff_block_via_activity(self) -> None:
        diff = (
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -1 +1 @@\n"
            '+AWS_KEY = "AKIAABCDEFGHIJKL2345"\n'
        )
        result = asyncio.run(precommit_scanner(diff))
        assert result.decision == "block"
        assert "aws_access_key" in result.matched_patterns


# ---------------------------------------------------------------------------
# Test 3 - injected Atlassian token  block + ``atlassian_api_token`` matched
# ---------------------------------------------------------------------------


class TestAtlassianApiTokenIsBlocked:
    """An injected Atlassian API token literal triggers a block decision."""

    def test_atlassian_api_token_blocks(self) -> None:
        # Documented Atlassian API token shape: ``ATATT3x`` prefix + body.
        diff = "JIRA_TOKEN = ATATT3xFfGF0c2VjcmV0X3Rva2VuX2Rlbm9fbm90X3JlYWw"
        result = scan_diff(diff)
        assert result.decision == "block"
        assert "atlassian_api_token" in result.matched_patterns

    def test_atlassian_token_in_diff_block_via_activity(self) -> None:
        diff = (
            "--- a/secrets.env\n"
            "+++ b/secrets.env\n"
            "@@ -1 +1 @@\n"
            "+JIRA=ATATT3xVeryLongAtlassianTokenContent_With-Dashes\n"
        )
        result = asyncio.run(precommit_scanner(diff))
        assert result.decision == "block"
        assert "atlassian_api_token" in result.matched_patterns


# ---------------------------------------------------------------------------
# Test 4 - determinism: same diff  identical ScanResult
# ---------------------------------------------------------------------------


class TestScanIsDeterministic:
    """``precommit_scanner(diff) == precommit_scanner(diff)`` for any diff."""

    @pytest.mark.parametrize(
        "diff",
        [
            "",
            "no secrets here",
            'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"',
            "ATATT3xFfGF0c2VjcmV0X3Rva2VuX2Rlbm9fbm90X3JlYWw",
            'header = "Bearer abc.def.ghi"',
            'password = "hunter2"',
            (
                'aws = "AKIAIOSFODNN7EXAMPLE"\n'
                "atlassian = ATATT3xFfGF0c2VjcmV0X3Rva2VuX2Rlbm9fbm90X3JlYWw\n"
                'header = "Bearer abc.def.ghi"\n'
                'password = "hunter2"\n'
            ),
        ],
    )
    def test_pure_scan_is_deterministic(self, diff: str) -> None:
        first = scan_diff(diff)
        second = scan_diff(diff)
        assert first == second
        # Frozen dataclass field-by-field equality (defensive - equal
        # dataclasses can still differ on identity / field ordering
        # subtleties; this catches accidental tuple reordering).
        assert first.decision == second.decision
        assert first.matched_patterns == second.matched_patterns

    def test_activity_entrypoint_is_deterministic_on_block(self) -> None:
        """Two invocations of the async activity yield identical results."""
        diff = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'
        first = asyncio.run(precommit_scanner(diff))
        second = asyncio.run(precommit_scanner(diff))
        assert first == second
        assert first.decision == "block"
        assert first.matched_patterns == ("aws_access_key",)

    def test_activity_entrypoint_is_deterministic_on_pass(self) -> None:
        """Determinism holds on the ``pass`` path too."""
        diff = "completely innocent diff"
        first = asyncio.run(precommit_scanner(diff))
        second = asyncio.run(precommit_scanner(diff))
        assert first == second
        assert first.decision == "pass"
        assert first.matched_patterns == ()

    def test_matched_patterns_are_sorted_for_stability(self) -> None:
        """Multi-pattern diffs return a sorted, deduplicated tuple."""
        diff = (
            'aws = "AKIAIOSFODNN7EXAMPLE"\n'
            "atlassian = ATATT3xFfGF0c2VjcmV0X3Rva2VuX2Rlbm9fbm90X3JlYWw\n"
            'header = "Bearer abc.def.ghi"\n'
            'password = "hunter2"\n'
        )
        result = scan_diff(diff)
        assert result.decision == "block"
        # Alphabetical order keeps multi-pattern results stable.
        assert result.matched_patterns == (
            "atlassian_api_token",
            "aws_access_key",
            "bearer_token",
            "generic_password",
        )


# ---------------------------------------------------------------------------
# Sanity - audit constant exposed for downstream dashboards
# ---------------------------------------------------------------------------


def test_audit_action_constant_is_stable() -> None:
    """The Streamlit security dashboard greps for this exact string."""
    assert PRECOMMIT_AUDIT_ACTION == "precommit_secret_leak_blocked"
