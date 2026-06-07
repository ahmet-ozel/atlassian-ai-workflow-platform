"""Unit tests for ``temporal_shared.noop_formatter``.

Validates the pure :func:`format_noop_result_comment` Jira-comment
formatter against worked examples: success with ``exit_code=0`` and
``stdout="ok\\n"``, failure with the exit code listed, and truncation
above 1024 characters.

The module is **pure**: every test below constructs inputs locally,
calls the formatter, and asserts on the returned string - no clocks,
no I/O, no fixtures.  This mirrors the test style of
``test_confluence_dedup.py`` (the closest existing analogue in this
package).

"""

from __future__ import annotations

import inspect

import pytest

from temporal_shared.noop_formatter import (
    NOOP_EXIT_CODE_UNKNOWN,
    NOOP_FAILURE_PREFIX,
    NOOP_STDOUT_TRUNCATE_CHARS,
    NOOP_SUCCESS_PREFIX,
    NOOP_TRUNCATION_MARKER,
    format_noop_result_comment,
)


# ---------------------------------------------------------------------------
# Module-level invariants - pinned constants
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """The pinned constants are part of the formatter contract."""

    def test_truncation_cap_is_1024(self) -> None:
        """

        The stdout truncation cap is fixed at 1024 characters so the
        comment renders cleanly in Jira.
        """
        assert NOOP_STDOUT_TRUNCATE_CHARS == 1024

    def test_success_prefix_is_pinned(self) -> None:
        """

        Downstream tooling (smoke-test scrapers, audit log readers)
        match Jira comments by this prefix; the literal text must
        not drift without an explicit migration.
        """
        assert NOOP_SUCCESS_PREFIX == " noop_test sonucu"

    def test_failure_prefix_is_pinned(self) -> None:
        assert NOOP_FAILURE_PREFIX == " noop_test sonucu"

    def test_unknown_exit_code_marker_is_pinned(self) -> None:
        assert NOOP_EXIT_CODE_UNKNOWN == "n/a"

    def test_truncation_marker_is_pinned(self) -> None:
        """

        The marker is rendered verbatim inside a fenced code block
        - its leading ellipsis character makes it visually distinct
        from the captured runner output.
        """
        assert NOOP_TRUNCATION_MARKER == "… [truncated]"


# ---------------------------------------------------------------------------
# Success path  (exit_code == 0)
# ---------------------------------------------------------------------------


class TestSuccessCases:
    """Concrete examples for the success branch."""

    def test_canonical_success_with_ok_stdout(self) -> None:
        """

        ``exit_code=0`` with ``stdout="ok\\n"`` produces the
        success comment.
        """
        body = format_noop_result_comment(exit_code=0, stdout="ok\n")
        assert body.startswith(NOOP_SUCCESS_PREFIX)
        assert "exit_code=0" in body
        # The fenced code block preserves the trailing newline so the
        # Jira preview shows the runner output verbatim.
        assert "```\nok\n\n```" in body

    def test_success_without_trailing_newline(self) -> None:
        """

        ``echo -n "ok"`` produces output without a trailing newline;
        the formatter still renders it inside a code block.
        """
        body = format_noop_result_comment(exit_code=0, stdout="ok")
        assert body == (
            " noop_test sonucu: exit_code=0, çıktı:\n"
            "```\nok\n```"
        )

    def test_success_with_empty_stdout_renders_no_output_marker(self) -> None:
        """

        Empty stdout collapses to ``çıktı: <yok>`` (no fenced block)
        so a Jira reader does not see an empty code block.
        """
        body = format_noop_result_comment(exit_code=0, stdout="")
        assert body == " noop_test sonucu: exit_code=0, çıktı: <yok>"
        assert "```" not in body

    def test_success_with_none_stdout_renders_no_output_marker(self) -> None:
        """

        ``stdout=None`` (e.g. the runner did not surface a payload)
        renders identically to the empty-string case.
        """
        body = format_noop_result_comment(exit_code=0, stdout=None)
        assert body == " noop_test sonucu: exit_code=0, çıktı: <yok>"

    def test_success_no_trailing_newline_in_comment(self) -> None:
        """

        Jira's markdown renderer adds its own paragraph spacing; a
        trailing newline on our side would produce an awkward gap.
        """
        body = format_noop_result_comment(exit_code=0, stdout="ok\n")
        assert not body.endswith("\n")


# ---------------------------------------------------------------------------
# Failure path  (exit_code != 0  or  exit_code is None)
# ---------------------------------------------------------------------------


class TestFailureCases:
    """Concrete examples for the failure branch."""

    def test_non_zero_exit_code_renders_failure_prefix(self) -> None:
        """

        The failure comment lists the exit code so the reader can
        correlate it with runner logs.
        """
        body = format_noop_result_comment(exit_code=1, stdout="boom")
        assert body.startswith(NOOP_FAILURE_PREFIX)
        assert "exit_code=1" in body
        assert "```\nboom\n```" in body

    def test_negative_exit_code_renders_verbatim(self) -> None:
        """

        Some runners surface signal-style negative exit codes
        (``-9`` for SIGKILL).  The formatter passes the integer
        through verbatim so the reader can recognise the signal.
        """
        body = format_noop_result_comment(exit_code=-9, stdout="killed")
        assert body.startswith(NOOP_FAILURE_PREFIX)
        assert "exit_code=-9" in body

    def test_large_exit_code_renders_verbatim(self) -> None:
        body = format_noop_result_comment(exit_code=255, stdout="")
        assert body == " noop_test sonucu: exit_code=255, çıktı: <yok>"

    def test_none_exit_code_renders_unknown_marker_and_failure_prefix(
        self,
    ) -> None:
        """

        A missing exit code (e.g. workflow timed out before the SSH
        command completed) is treated as failure with a distinctive
        ``n/a`` marker so the reader can tell "no exit code" apart
        from ``exit_code=0``.
        """
        body = format_noop_result_comment(exit_code=None, stdout="ok")
        assert body.startswith(NOOP_FAILURE_PREFIX)
        assert f"exit_code={NOOP_EXIT_CODE_UNKNOWN}" in body
        assert "```\nok\n```" in body

    def test_failure_with_empty_stdout(self) -> None:
        """

        A failed run that produced no stdout still renders the
        ``<yok>`` marker - the failure prefix alone signals the
        outcome.
        """
        body = format_noop_result_comment(exit_code=1, stdout="")
        assert body == " noop_test sonucu: exit_code=1, çıktı: <yok>"


# ---------------------------------------------------------------------------
# Truncation cap  (NOOP_STDOUT_TRUNCATE_CHARS = 1024)
# ---------------------------------------------------------------------------


class TestTruncation:
    """Concrete examples for the 1024-character cap."""

    def test_stdout_at_cap_is_not_truncated(self) -> None:
        """

        Output exactly at the cap is preserved verbatim - the
        truncation marker only fires when the cap is *exceeded*.
        """
        stdout = "a" * NOOP_STDOUT_TRUNCATE_CHARS
        body = format_noop_result_comment(exit_code=0, stdout=stdout)
        assert NOOP_TRUNCATION_MARKER not in body
        assert stdout in body

    def test_stdout_just_above_cap_is_truncated(self) -> None:
        """

        One character over the cap is enough to trigger the marker
        - the cap is strict by design.
        """
        stdout = "a" * (NOOP_STDOUT_TRUNCATE_CHARS + 1)
        body = format_noop_result_comment(exit_code=0, stdout=stdout)
        assert NOOP_TRUNCATION_MARKER in body

    def test_truncation_preserves_first_n_chars(self) -> None:
        """

        The leading 1024 characters survive intact so a Jira reader
        sees the actual start of the runner output before the
        marker appears.
        """
        head = "head-" + "x" * (NOOP_STDOUT_TRUNCATE_CHARS - 5)
        tail = "y" * 100
        stdout = head + tail
        body = format_noop_result_comment(exit_code=0, stdout=stdout)
        assert head in body
        # The tail bytes must not leak past the truncation cap.
        assert tail not in body

    def test_truncation_marker_is_inside_code_block(self) -> None:
        """

        The fenced code block must close *after* the marker so a
        Jira reader sees the marker rendered as code rather than
        as inline prose.
        """
        stdout = "z" * (NOOP_STDOUT_TRUNCATE_CHARS + 50)
        body = format_noop_result_comment(exit_code=0, stdout=stdout)
        marker_idx = body.index(NOOP_TRUNCATION_MARKER)
        closing_fence_idx = body.rindex("```")
        assert marker_idx < closing_fence_idx, (
            "truncation marker must be rendered inside the fenced "
            "code block, not after it"
        )

    def test_multibyte_characters_count_as_one(self) -> None:
        """

        The cap operates on **characters**, not bytes.  Turkish
        characters in the captured output count as one each, which
        is the correct unit for Jira's preview width.
        """
        # 1024 Turkish "ı" characters fits inside the cap on a
        # character basis even though the UTF-8 byte count is
        # larger.
        stdout = "ı" * NOOP_STDOUT_TRUNCATE_CHARS
        body = format_noop_result_comment(exit_code=0, stdout=stdout)
        assert NOOP_TRUNCATION_MARKER not in body

        # One character over the cap  truncated, regardless of
        # byte count.
        stdout = "ı" * (NOOP_STDOUT_TRUNCATE_CHARS + 1)
        body = format_noop_result_comment(exit_code=0, stdout=stdout)
        assert NOOP_TRUNCATION_MARKER in body


# ---------------------------------------------------------------------------
# Type-validation paths
# ---------------------------------------------------------------------------


class TestTypeValidation:
    """The formatter rejects malformed inputs at the boundary."""

    def test_bool_exit_code_is_rejected(self) -> None:
        """

        ``isinstance(True, int)`` is True in Python; the formatter
        rejects booleans explicitly so a wrong call site does not
        render ``exit_code=True``.
        """
        with pytest.raises(TypeError, match="exit_code must be int or None"):
            format_noop_result_comment(
                exit_code=True,  # type: ignore[arg-type]
                stdout="ok",
            )

    def test_string_exit_code_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="exit_code must be int or None"):
            format_noop_result_comment(
                exit_code="0",  # type: ignore[arg-type]
                stdout="ok",
            )

    def test_float_exit_code_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="exit_code must be int or None"):
            format_noop_result_comment(
                exit_code=0.0,  # type: ignore[arg-type]
                stdout="ok",
            )

    def test_bytes_stdout_is_rejected(self) -> None:
        """

        The activity must decode the runner's bytes payload before
        passing it through; passing raw bytes would silently render
        as ``b'ok'`` in the comment.
        """
        with pytest.raises(TypeError, match="stdout must be str or None"):
            format_noop_result_comment(
                exit_code=0,
                stdout=b"ok",  # type: ignore[arg-type]
            )

    def test_int_stdout_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="stdout must be str or None"):
            format_noop_result_comment(
                exit_code=0,
                stdout=42,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Determinism / purity
# ---------------------------------------------------------------------------


class TestPurity:
    """The formatter must be a pure function of its inputs."""

    def test_repeated_calls_produce_identical_output(self) -> None:
        """

        Two calls with identical inputs must return identical
        strings - the formatter has no hidden state.
        """
        first = format_noop_result_comment(exit_code=0, stdout="ok\n")
        second = format_noop_result_comment(exit_code=0, stdout="ok\n")
        assert first == second

    def test_module_does_not_import_clocks_or_randomness(self) -> None:
        """

        A workflow / activity that imported ``time`` / ``random`` /
        ``uuid`` here would fail the AST-based replay-determinism
        property test.  Asserting the source text catches
        the violation early with a clear error message.
        """
        from temporal_shared import noop_formatter

        source = inspect.getsource(noop_formatter)
        forbidden = (
            "import time",
            "import random",
            "import uuid",
            "from datetime",
            "import datetime",
        )
        for needle in forbidden:
            assert needle not in source, (
                f"noop_formatter must not import {needle!r} - it would "
                "couple the comment text to a clock or RNG and break "
                "replay determinism when invoked from a Temporal "
                "workflow."
            )


# ---------------------------------------------------------------------------
# Re-export check  (temporal_shared package surface)
# ---------------------------------------------------------------------------


class TestPackageReexports:
    """The public formatter is reachable through the package facade."""

    def test_format_function_is_reexported(self) -> None:
        """

        Call sites import from :mod:`temporal_shared`; if the
        re-export is dropped, every consumer breaks at import time.
        Pinning the export here catches a regression early.
        """
        import temporal_shared

        assert hasattr(temporal_shared, "format_noop_result_comment")
        assert (
            temporal_shared.format_noop_result_comment
            is format_noop_result_comment
        )

    def test_constants_are_reexported(self) -> None:
        import temporal_shared

        assert (
            temporal_shared.NOOP_STDOUT_TRUNCATE_CHARS
            == NOOP_STDOUT_TRUNCATE_CHARS
        )
        assert temporal_shared.NOOP_SUCCESS_PREFIX == NOOP_SUCCESS_PREFIX
        assert temporal_shared.NOOP_FAILURE_PREFIX == NOOP_FAILURE_PREFIX
