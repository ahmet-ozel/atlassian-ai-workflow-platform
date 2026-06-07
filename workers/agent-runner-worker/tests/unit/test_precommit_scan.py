"""Unit tests for the ``precommit_scanner`` activity.

Covers the pure :func:`scan_diff` core and the audit-emission layer
of :func:`precommit_scanner` against the four documented secret
patterns (AWS access key, Atlassian API token, Bearer header,
hard-coded password).

The property tests own the hypothesis-driven determinism / clean-diff
invariants. This
unit test exists to pin specific examples for fast regression
detection and to verify the audit side-effect layer.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the worker ``src`` package importable as ``src.activities.precommit_scan``
# (mirrors the pattern used by ``test_minio_activity.py`` /
# ``test_vault_activity.py`` in the execution-runner-worker tests).
_WORKER_ROOT = Path(__file__).resolve().parents[2]  # workers/agent-runner-worker/
_WORKER_SRC = _WORKER_ROOT / "src"
_WORKER_PARENT = _WORKER_ROOT  # contains ``src`` as a sub-package

for _path in (_WORKER_PARENT, _WORKER_SRC):
    _str = str(_path)
    if _str not in sys.path:
        sys.path.insert(0, _str)

# Wire the foundation libs onto sys.path so ``from audit_logger import ...``
# works under the worker's unit test runner without first installing the libs.
_PLATFORM_ROOT = _WORKER_ROOT.parents[1]  # platform/
for _lib in ("audit_logger",):
    _lib_src = _PLATFORM_ROOT / "libs" / _lib / "src"
    _lib_str = str(_lib_src)
    if _lib_src.is_dir() and _lib_str not in sys.path:
        sys.path.insert(0, _lib_str)

from src.activities.precommit_scan import (  # noqa: E402
    PRECOMMIT_AUDIT_ACTION,
    SECRET_PATTERNS,
    ScanResult,
    get_audit_logger,
    precommit_scanner,
    scan_diff,
    set_audit_logger,
)


# ---------------------------------------------------------------------------
# scan_diff - pure core
# ---------------------------------------------------------------------------


class TestScanDiffClean:
    """``scan_diff`` returns ``decision='pass'`` for diffs without secrets."""

    def test_empty_diff_is_pass(self) -> None:
        result = scan_diff("")
        assert result == ScanResult(decision="pass", matched_patterns=())

    def test_innocuous_python_diff_is_pass(self) -> None:
        diff = (
            "--- a/src/util.py\n"
            "+++ b/src/util.py\n"
            "@@ -1,3 +1,3 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return a + b  # explicit return\n"
        )
        result = scan_diff(diff)
        assert result.decision == "pass"
        assert result.matched_patterns == ()

    def test_word_bearer_in_unrelated_context_does_not_match(self) -> None:
        # ``Bearer`` followed by something other than whitespace+token
        # must not fire (the pattern requires the actual auth-header
        # shape, not the word in prose).
        diff = "MyBearerWrapperFactory class is documented here."
        result = scan_diff(diff)
        assert result.decision == "pass"


class TestScanDiffSecrets:
    """Every documented secret pattern fires with the correct name."""

    def test_aws_access_key_is_blocked(self) -> None:
        diff = 'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"'
        result = scan_diff(diff)
        assert result.decision == "block"
        assert "aws_access_key" in result.matched_patterns

    def test_atlassian_api_token_is_blocked(self) -> None:
        # The fixed ``ATATT3x`` prefix + non-empty body satisfies the
        # documented Atlassian token regex.
        diff = "JIRA_TOKEN = ATATT3xFfGF0c2VjcmV0X3Rva2VuX2Rlbm9fbm90X3JlYWw="
        result = scan_diff(diff)
        assert result.decision == "block"
        assert "atlassian_api_token" in result.matched_patterns

    def test_bearer_token_header_is_blocked(self) -> None:
        diff = 'headers = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"}'
        result = scan_diff(diff)
        assert result.decision == "block"
        assert "bearer_token" in result.matched_patterns

    def test_generic_password_is_blocked(self) -> None:
        diff = 'password = "hunter2"'
        result = scan_diff(diff)
        assert result.decision == "block"
        assert "generic_password" in result.matched_patterns

    def test_generic_password_is_case_insensitive(self) -> None:
        diff = 'PASSWORD = "hunter2"'
        result = scan_diff(diff)
        assert result.decision == "block"
        assert "generic_password" in result.matched_patterns

    def test_multiple_secrets_yield_sorted_unique_names(self) -> None:
        diff = (
            'aws = "AKIAIOSFODNN7EXAMPLE"\n'
            'header = "Bearer abc.def.ghi"\n'
            'password = "hunter2"\n'
        )
        result = scan_diff(diff)
        assert result.decision == "block"
        # Sorted alphabetically for deterministic comparisons.
        assert result.matched_patterns == (
            "aws_access_key",
            "bearer_token",
            "generic_password",
        )

    def test_same_pattern_multiple_locations_dedupes(self) -> None:
        # Two AWS keys → still a single ``aws_access_key`` entry.
        diff = (
            'a = "AKIAIOSFODNN7EXAMPLE"\n'
            'b = "AKIAABCDEFGHIJKL2345"\n'
        )
        result = scan_diff(diff)
        assert result.decision == "block"
        assert result.matched_patterns == ("aws_access_key",)


class TestScanDiffDeterminism:
    """``scan_diff(d) == scan_diff(d)`` for every diff."""

    @pytest.mark.parametrize(
        "diff",
        [
            "",
            "no secrets here",
            'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"',
            'Bearer abc.def.ghi\npassword = "hunter2"',
            "ATATT3xVeryLongAtlassianTokenContent_With-Dashes",
        ],
    )
    def test_repeated_calls_yield_identical_result(self, diff: str) -> None:
        a = scan_diff(diff)
        b = scan_diff(diff)
        assert a == b
        # Frozen dataclass instances compare equal but are distinct
        # objects (the activity does not memoise its return value).
        assert a.decision == b.decision
        assert a.matched_patterns == b.matched_patterns


class TestScanDiffInputValidation:
    """Non-string input is rejected fast."""

    @pytest.mark.parametrize("bad", [None, 123, b"AKIAIOSFODNN7EXAMPLE", ["AKIA..."]])
    def test_non_string_raises_typeerror(self, bad: Any) -> None:
        with pytest.raises(TypeError):
            scan_diff(bad)


# ---------------------------------------------------------------------------
# Pattern table sanity
# ---------------------------------------------------------------------------


class TestSecretPatternsTable:
    """Static guarantees about the exported ``SECRET_PATTERNS`` mapping."""

    def test_table_contains_required_p0_patterns(self) -> None:
        # The scanner covers AWS, Atlassian, Bearer, and password patterns.
        required = {
            "aws_access_key",
            "atlassian_api_token",
            "bearer_token",
            "generic_password",
        }
        assert required.issubset(SECRET_PATTERNS.keys())

    def test_audit_action_constant_is_stable(self) -> None:
        # The dashboard / Streamlit security page greps for this string.
        assert PRECOMMIT_AUDIT_ACTION == "precommit_secret_leak_blocked"


# ---------------------------------------------------------------------------
# precommit_scanner - audit emission layer
# ---------------------------------------------------------------------------


class _CapturingAuditLogger:
    """Tiny duck-typed ``AuditLogger`` substitute for unit tests.

    Mirrors the production ``async write(event)`` contract and stashes
    the events on ``self.events`` for assertions.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.fail_next: bool = False

    async def write(self, event: Any) -> None:  # noqa: ANN401 - duck-typed
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated audit pipeline failure")
        self.events.append(event)


@pytest.fixture(autouse=True)
def _reset_audit_logger() -> Any:
    """Ensure each test starts with no audit logger wired."""

    # Capture and restore the current value so tests don't leak state.
    previous = get_audit_logger()
    set_audit_logger(None)
    yield
    set_audit_logger(previous)


def test_precommit_scanner_pass_does_not_emit_audit() -> None:
    """A ``pass`` decision MUST NOT write to the audit log."""
    import asyncio

    capturing = _CapturingAuditLogger()
    set_audit_logger(capturing)

    result = asyncio.run(precommit_scanner("clean diff with no secrets"))

    assert result.decision == "pass"
    assert capturing.events == []


def test_precommit_scanner_block_emits_single_audit_event() -> None:
    """A ``block`` decision MUST emit one ``precommit_secret_leak_blocked`` row."""
    import asyncio

    capturing = _CapturingAuditLogger()
    set_audit_logger(capturing)

    diff = 'password = "hunter2"'
    result = asyncio.run(precommit_scanner(diff))

    assert result.decision == "block"
    assert result.matched_patterns == ("generic_password",)
    assert len(capturing.events) == 1

    event = capturing.events[0]
    assert event.action == PRECOMMIT_AUDIT_ACTION
    assert event.actor_role == "system"
    assert event.result == "denied"
    # Payload carries the matched pattern names - never the secret values.
    assert event.payload is not None
    assert "matched_patterns" in event.payload
    assert event.payload["matched_patterns"] == ["generic_password"]
    assert "hunter2" not in str(event.payload)


def test_precommit_scanner_block_survives_audit_failure() -> None:
    """A failing audit pipeline MUST NOT swallow the ``block`` decision."""
    import asyncio

    capturing = _CapturingAuditLogger()
    capturing.fail_next = True
    set_audit_logger(capturing)

    diff = 'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"'
    result = asyncio.run(precommit_scanner(diff))

    # The gate decision survives the audit-write failure.
    assert result.decision == "block"
    assert "aws_access_key" in result.matched_patterns
    # No event recorded because the simulated write raised.
    assert capturing.events == []


def test_precommit_scanner_block_with_no_audit_logger_is_silent() -> None:
    """When no audit logger is wired the scan still produces the correct result."""
    import asyncio

    set_audit_logger(None)

    diff = 'Authorization: Bearer abc.def.ghi'
    result = asyncio.run(precommit_scanner(diff))

    assert result.decision == "block"
    assert "bearer_token" in result.matched_patterns
