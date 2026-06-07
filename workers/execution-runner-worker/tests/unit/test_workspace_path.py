"""Unit tests for :mod:`src.runners.workspace_path`.

The exhaustive Hypothesis-based property test that exercises the full input
space lives in ``platform/tests/property/test_runner_workspace_path.py``;
this file documents the helper's example-level contract so a developer can
``pytest tests/unit -k workspace_path`` and get fast feedback while editing.
"""

from __future__ import annotations

import pytest

from src.runners.workspace_path import (
    MAX_ITER,
    InvalidIssueKeyError,
    InvalidIterError,
    build_workspace_path,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestBuildWorkspacePathHappy:
    def test_canonical_output(self) -> None:
        assert (
            build_workspace_path("/var/ai-runner", "PAY-4211", 0)
            == "/var/ai-runner/PAY-4211/iter-0"
        )

    def test_underscored_project_key(self) -> None:
        assert (
            build_workspace_path("/var/ai-runner", "OPS_CORE-12", 3)
            == "/var/ai-runner/OPS_CORE-12/iter-3"
        )

    def test_trailing_slash_in_base_normalised(self) -> None:
        # Multiple trailing slashes collapse to a single separator so callers
        # can pass either ``/var/ai-runner`` or ``/var/ai-runner/`` without
        # producing ``/var/ai-runner//PAY-1/iter-0``.
        assert (
            build_workspace_path("/var/ai-runner/", "PAY-1", 0)
            == "/var/ai-runner/PAY-1/iter-0"
        )
        assert (
            build_workspace_path("/var/ai-runner///", "PAY-1", 0)
            == "/var/ai-runner/PAY-1/iter-0"
        )

    def test_max_iter_inclusive(self) -> None:
        assert build_workspace_path("/x", "A-1", MAX_ITER) == f"/x/A-1/iter-{MAX_ITER}"

    def test_min_iter_inclusive(self) -> None:
        assert build_workspace_path("/x", "A-1", 0) == "/x/A-1/iter-0"

    def test_deterministic(self) -> None:
        # Same inputs  same output, byte-for-byte.
        a = build_workspace_path("/var/ai-runner", "PAY-4211", 7)
        b = build_workspace_path("/var/ai-runner", "PAY-4211", 7)
        assert a == b


# ---------------------------------------------------------------------------
# issue_key validation for path-traversal safety
# ---------------------------------------------------------------------------


class TestBuildWorkspacePathIssueKeyGuard:
    @pytest.mark.parametrize(
        "bad_key",
        [
            "../etc",                # path traversal
            "../../etc/passwd",      # deeper traversal
            "PAY-4211/../OTHER-1",   # embedded traversal
            "PAY-4211;rm -rf /",     # shell metachar (semicolon)
            "PAY-4211 && whoami",    # shell metachar (and)
            "PAY-4211|cat",          # shell metachar (pipe)
            "PAY-4211`id`",          # backtick injection
            "PAY-4211$HOME",         # variable expansion
            "PAY-4211\nRM",          # newline injection
            "PAY-4211\x00",          # null-byte injection
            "pay-4211",              # lowercase project key
            "PAY",                   # missing -N
            "PAY-",                  # missing digits
            "-4211",                 # missing project key
            "1PAY-1",                # leading digit
            "PAY 4211",              # space instead of dash
            "",                      # empty string
        ],
    )
    def test_rejects_invalid(self, bad_key: str) -> None:
        with pytest.raises(InvalidIssueKeyError) as exc_info:
            build_workspace_path("/var/ai-runner", bad_key, 0)
        # Audit payload should expose the offending value verbatim.
        assert exc_info.value.issue_key == bad_key

    def test_rejects_non_string(self) -> None:
        with pytest.raises(InvalidIssueKeyError):
            build_workspace_path("/var/ai-runner", 123, 0)  # type: ignore[arg-type]

    def test_rejects_none(self) -> None:
        with pytest.raises(InvalidIssueKeyError):
            build_workspace_path("/var/ai-runner", None, 0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# iter_n validation
# ---------------------------------------------------------------------------


class TestBuildWorkspacePathIterGuard:
    @pytest.mark.parametrize("bad_iter", [-1, -1000, MAX_ITER + 1, 10_000])
    def test_rejects_out_of_range(self, bad_iter: int) -> None:
        with pytest.raises(InvalidIterError) as exc_info:
            build_workspace_path("/var/ai-runner", "PAY-1", bad_iter)
        assert exc_info.value.iter_n == bad_iter

    @pytest.mark.parametrize("bad_iter", ["0", 1.5, None, [0]])
    def test_rejects_non_int(self, bad_iter: object) -> None:
        with pytest.raises(InvalidIterError):
            build_workspace_path("/var/ai-runner", "PAY-1", bad_iter)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        # ``isinstance(True, int)`` is ``True`` in Python; the helper guards
        # against this so iter=True doesn't silently render as ``iter-1``.
        with pytest.raises(InvalidIterError):
            build_workspace_path("/var/ai-runner", "PAY-1", True)  # type: ignore[arg-type]
        with pytest.raises(InvalidIterError):
            build_workspace_path("/var/ai-runner", "PAY-1", False)  # type: ignore[arg-type]
