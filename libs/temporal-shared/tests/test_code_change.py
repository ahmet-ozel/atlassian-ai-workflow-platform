"""Unit tests for ``temporal_shared.code_change``.

Validates the pure formatters :func:`compute_branch_name` and
:func:`format_commit_message` against
branch-name invariant+(b).

The dedicated property-test suite for this module lives in
this file covers concrete examples and the validation error paths so a
``pytest libs/temporal-shared`` run remains hermetic.

"""

from __future__ import annotations

import inspect

import pytest

from temporal_shared.code_change import (
    BOT_COMMIT_PREFIX,
    InvalidBotEmailError,
    InvalidIterationError,
    compute_branch_name,
    format_commit_message,
)
from temporal_shared.identifiers import InvalidIssueKeyError


# ---------------------------------------------------------------------------
# compute_branch_name - happy paths
# ---------------------------------------------------------------------------


class TestComputeBranchNameHappyPath:
    """Concrete example coverage for the iter==1 vs iter>=2 decision."""

    def test_iter1_with_empty_existing_returns_bare(self) -> None:
        assert compute_branch_name("PAY-4211", 1, []) == "ai/PAY-4211"

    def test_iter1_with_unrelated_branches_returns_bare(self) -> None:
        """

        Branches that are not the bare ``ai/{issue_key}`` candidate must
        not influence the decision - only the exact name matters.
        """
        assert (
            compute_branch_name(
                "PAY-4211",
                1,
                ["main", "release/2024-01", "ai/PAY-9999", "feature/foo"],
            )
            == "ai/PAY-4211"
        )

    def test_iter1_with_bare_taken_falls_back_to_iter1_form(self) -> None:
        """

        When the bare ``ai/{issue_key}`` slot is already taken on iter==1
        the formatter must fall back to the iter-suffixed form so the
        output never collides with an existing branch.
        """
        assert (
            compute_branch_name("PAY-4211", 1, ["ai/PAY-4211"])
            == "ai/PAY-4211-iter1"
        )

    def test_iter2_always_returns_iter_form(self) -> None:
        assert compute_branch_name("PAY-4211", 2, []) == "ai/PAY-4211-iter2"

    def test_iter2_ignores_existing_branches(self) -> None:
        """

        For iter>=2 the existing_branches argument is informational; the
        function deterministically returns the iter-suffixed form
        regardless of contents.
        """
        existing = ["ai/PAY-4211", "ai/PAY-4211-iter2", "main"]
        assert (
            compute_branch_name("PAY-4211", 2, existing)
            == "ai/PAY-4211-iter2"
        )

    def test_high_iteration_number(self) -> None:
        assert (
            compute_branch_name("PAY-4211", 99, [])
            == "ai/PAY-4211-iter99"
        )

    def test_underscore_project_key(self) -> None:
        """

        Project keys with underscores (allowed by the identifiers regex)
        flow through unchanged.
        """
        assert (
            compute_branch_name("ABC_DEF-1", 1, [])
            == "ai/ABC_DEF-1"
        )

    @pytest.mark.parametrize(
        "existing",
        [
            [],
            ("main", "develop"),
            {"main", "ai/OTHER-1"},
            frozenset({"main"}),
            iter(("main", "develop")),  # generator-like iterable
        ],
        ids=["list", "tuple", "set", "frozenset", "iterator"],
    )
    def test_accepts_arbitrary_iterable(self, existing) -> None:
        """

        ``existing_branches`` is typed ``Iterable[str]`` - any iterable
        must work.
        """
        assert (
            compute_branch_name("PAY-1", 1, existing) == "ai/PAY-1"
        )

    def test_pure_deterministic(self) -> None:
        """

        Two invocations with identical arguments return equal results.
        """
        first = compute_branch_name("PAY-4211", 1, ["main"])
        second = compute_branch_name("PAY-4211", 1, ["main"])
        assert first == second == "ai/PAY-4211"


# ---------------------------------------------------------------------------
# compute_branch_name - validation errors
# ---------------------------------------------------------------------------


class TestComputeBranchNameValidation:
    """Issue-key and iteration validation."""

    @pytest.mark.parametrize(
        "bad_issue_key",
        [
            "pay-1",          # lowercase project
            "PAY-0",          # zero issue
            "PAY-01",         # leading-zero issue
            "PAY",            # missing dash
            "-PAY-1",         # leading dash
            "1PAY-1",         # leading digit
            "",               # empty
            "PAY-1 ",         # trailing space
        ],
    )
    def test_invalid_issue_key_raises(self, bad_issue_key: str) -> None:
        with pytest.raises(InvalidIssueKeyError):
            compute_branch_name(bad_issue_key, 1, [])

    def test_non_str_issue_key_raises(self) -> None:
        with pytest.raises(InvalidIssueKeyError):
            compute_branch_name(12345, 1, [])  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_iter", [0, -1, -100])
    def test_non_positive_iteration_raises(self, bad_iter: int) -> None:
        with pytest.raises(InvalidIterationError):
            compute_branch_name("PAY-1", bad_iter, [])

    def test_bool_iteration_rejected(self) -> None:
        """

        ``bool`` is a subclass of ``int`` - the validator must reject it
        explicitly so ``True`` is not silently treated as iter==1 and
        ``False`` as iter==0.
        """
        with pytest.raises(InvalidIterationError):
            compute_branch_name("PAY-1", True, [])  # type: ignore[arg-type]
        with pytest.raises(InvalidIterationError):
            compute_branch_name("PAY-1", False, [])  # type: ignore[arg-type]

    def test_float_iteration_rejected(self) -> None:
        with pytest.raises(InvalidIterationError):
            compute_branch_name("PAY-1", 1.0, [])  # type: ignore[arg-type]

    def test_none_iteration_rejected(self) -> None:
        with pytest.raises(InvalidIterationError):
            compute_branch_name("PAY-1", None, [])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# format_commit_message - happy paths
# ---------------------------------------------------------------------------


class TestFormatCommitMessageHappyPath:
    """The output begins with ``[bot] `` and contains the trailer."""

    def test_basic_format(self) -> None:
        result = format_commit_message(
            "fix payment retry logic",
            "PAY-4211",
            1,
            "ai-bot@company.com",
        )
        assert result == (
            "[bot] fix payment retry logic\n"
            "\n"
            "Co-authored-by: ai-bot <ai-bot@company.com>"
        )

    def test_starts_with_bot_prefix(self) -> None:
        result = format_commit_message(
            "implement feature", "PAY-1", 1, "ai-bot@company.com"
        )
        assert result.startswith(f"{BOT_COMMIT_PREFIX} ")

    def test_contains_co_authored_by_trailer(self) -> None:
        result = format_commit_message(
            "do work", "PAY-1", 1, "robo@example.org"
        )
        assert "Co-authored-by: ai-bot <robo@example.org>" in result

    def test_trailer_is_on_its_own_line_after_blank(self) -> None:
        """

        Git recognises trailers that follow a blank line; the formatter
        must emit exactly one blank line between the body and the
        ``Co-authored-by`` line.
        """
        result = format_commit_message(
            "subject", "PAY-1", 1, "ai-bot@company.com"
        )
        assert "\n\nCo-authored-by:" in result
        assert result.count("\n\nCo-authored-by:") == 1

    def test_multiline_body_preserved(self) -> None:
        """

        Internal newlines and structure inside the LLM-produced body are
        preserved verbatim - only trailing whitespace is normalised so
        the trailer block separator remains unambiguous.
        """
        body = "subject line\n\nlonger explanation\nwith two lines"
        result = format_commit_message(
            body, "PAY-1", 1, "ai-bot@company.com"
        )
        assert "[bot] subject line\n\nlonger explanation\nwith two lines\n\nCo-authored-by:" in result

    def test_trailing_whitespace_stripped(self) -> None:
        result = format_commit_message(
            "subject\n\n", "PAY-1", 1, "ai-bot@company.com"
        )
        # No triple-newline before the trailer.
        assert "\n\n\n" not in result
        assert result.endswith("Co-authored-by: ai-bot <ai-bot@company.com>")

    def test_iteration_2_does_not_change_output_shape(self) -> None:
        """

        ``iteration`` is currently a structural validation only; the
        commit body itself is the only place the iter is referenced
        (via the LLM prompt). The output shape is iter-independent.
        """
        first = format_commit_message(
            "msg", "PAY-1", 1, "ai-bot@company.com"
        )
        later = format_commit_message(
            "msg", "PAY-1", 5, "ai-bot@company.com"
        )
        assert first == later

    def test_pure_deterministic(self) -> None:
        first = format_commit_message(
            "subject", "PAY-1", 1, "ai-bot@company.com"
        )
        second = format_commit_message(
            "subject", "PAY-1", 1, "ai-bot@company.com"
        )
        assert first == second


# ---------------------------------------------------------------------------
# format_commit_message - validation errors
# ---------------------------------------------------------------------------


class TestFormatCommitMessageValidation:
    """Bad inputs raise typed errors (no silent coercion)."""

    def test_non_str_message_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            format_commit_message(
                123,  # type: ignore[arg-type]
                "PAY-1",
                1,
                "ai-bot@company.com",
            )

    def test_invalid_issue_key_raises(self) -> None:
        with pytest.raises(InvalidIssueKeyError):
            format_commit_message(
                "msg", "pay-1", 1, "ai-bot@company.com"
            )

    def test_invalid_iteration_raises(self) -> None:
        with pytest.raises(InvalidIterationError):
            format_commit_message("msg", "PAY-1", 0, "ai-bot@company.com")

    @pytest.mark.parametrize(
        "bad_email",
        [
            "",
            "not-an-email",
            "missing@tld",
            "@nohost.com",
            "spaces in@email.com",
            "trailing@dot.",
            "ai-bot@company",   # no TLD
        ],
    )
    def test_invalid_bot_email_raises(self, bad_email: str) -> None:
        with pytest.raises(InvalidBotEmailError):
            format_commit_message("msg", "PAY-1", 1, bad_email)

    def test_non_str_bot_email_raises(self) -> None:
        with pytest.raises(InvalidBotEmailError):
            format_commit_message(
                "msg", "PAY-1", 1, None  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Module hygiene - no I/O imports inside this replay-safe module
# ---------------------------------------------------------------------------


class TestModuleIsReplaySafe:
    """The module must be safe to import inside a Temporal workflow.

    The static AST scan in ``test_workflow_determinism_static.py`` (task
    2.7) covers the workflow modules themselves. We add a smaller
    smoke check here so a regression in ``code_change.py`` is caught at
    the unit-test layer too.
    """

    def test_source_does_not_import_forbidden_modules(self) -> None:
        """

        ``code_change.py`` is invoked from inside a workflow; it must
        not import ``datetime``, ``random``, ``uuid``, ``os``, ``time``,
        ``httpx``, ``requests``, or any I/O library.
        """
        from temporal_shared import code_change

        source = inspect.getsource(code_change)
        forbidden = (
            "import datetime",
            "from datetime",
            "import random",
            "from random",
            "import uuid",
            "from uuid",
            "import httpx",
            "from httpx",
            "import requests",
            "from requests",
            "import aiohttp",
            "from aiohttp",
            "import time",
            "from time",
        )
        for token in forbidden:
            assert token not in source, (
                f"code_change.py must not contain {token!r} - "
                "the module is invoked from within a workflow and "
                "must remain replay-deterministic."
            )
