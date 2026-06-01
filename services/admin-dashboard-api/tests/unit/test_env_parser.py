"""Unit tests for ``src.lifecycle.env_parser.parse_env_example``.

Validates the pure parse contract from design §4.7 and Requirements
5.1, 5.2, 5.4 — assignment recognition, comment-buffer accumulation,
blank-line reset, quote handling, ordering, and ``is_sensitive``
derivation.

The tests are colocated under ``tests/unit/`` (per task 3.1's
co-location convention) and exercise the parser as a black box; no
fixtures, no I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The admin-dashboard-api package ships its source under ``src/``; add
# the service root to ``sys.path`` so ``import src.lifecycle.env_parser``
# resolves under direct ``pytest tests/unit`` invocations.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.env_parser import EnvField, parse_env_example  # noqa: E402


# ---------------------------------------------------------------------------
# Empty / trivial inputs
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_list() -> None:
    """An empty string produces no fields (Requirement 5.2)."""

    assert parse_env_example("") == []


def test_whitespace_only_input_returns_empty_list() -> None:
    """Blank lines and pure whitespace yield no fields and never raise."""

    assert parse_env_example("\n\n   \n\t\n") == []


# ---------------------------------------------------------------------------
# Assignment recognition (Requirement 5.1, design §4.7 rule 1)
# ---------------------------------------------------------------------------


def test_single_assignment_no_comment() -> None:
    """A bare ``KEY=VALUE`` line yields one field with ``comment=None``."""

    fields = parse_env_example("PORT=8082\n")
    assert fields == [
        EnvField(key="PORT", default_value="8082", comment=None, is_sensitive=False)
    ]


def test_assignment_without_trailing_newline() -> None:
    """The final assignment is captured even when no trailing ``\\n`` exists."""

    fields = parse_env_example("LOG_LEVEL=INFO")
    assert len(fields) == 1
    assert fields[0].key == "LOG_LEVEL"
    assert fields[0].default_value == "INFO"


def test_multiple_assignments_preserve_file_order() -> None:
    """Field order matches first appearance in the source text (Requirement 5.4)."""

    text = "ALPHA=1\nBRAVO=2\nCHARLIE=3\n"
    fields = parse_env_example(text)
    assert [f.key for f in fields] == ["ALPHA", "BRAVO", "CHARLIE"]


def test_lowercase_keys_are_skipped_silently() -> None:
    """Design §4.7 limits assignments to ``^[A-Z][A-Z0-9_]*=.*$``."""

    # ``port=8082`` does not satisfy the uppercase-leading rule; the
    # parser drops it without raising.
    fields = parse_env_example("port=8082\nPORT=8082\n")
    assert [f.key for f in fields] == ["PORT"]


def test_keys_starting_with_digit_are_skipped() -> None:
    """Keys must start with a letter; ``9LIVES=1`` is malformed."""

    fields = parse_env_example("9LIVES=1\nNORMAL=2\n")
    assert [f.key for f in fields] == ["NORMAL"]


def test_empty_default_value_is_preserved() -> None:
    """``KEY=`` produces an :class:`EnvField` with ``default_value == ""``."""

    fields = parse_env_example("OPENAI_API_KEY=\n")
    assert fields[0].key == "OPENAI_API_KEY"
    assert fields[0].default_value == ""


def test_value_with_equals_sign_is_preserved_verbatim() -> None:
    """Only the *first* ``=`` separates key from value (greedy RHS)."""

    fields = parse_env_example("DATABASE_URL=postgresql://user:pass@host:5432/db?ssl=true\n")
    assert fields[0].default_value == "postgresql://user:pass@host:5432/db?ssl=true"


# ---------------------------------------------------------------------------
# Quote handling (design §4.7 quote handling rule)
# ---------------------------------------------------------------------------


def test_double_quoted_value_strips_quotes() -> None:
    fields = parse_env_example('GREETING="hello world"\n')
    assert fields[0].default_value == "hello world"


def test_single_quoted_value_strips_quotes() -> None:
    fields = parse_env_example("GREETING='hello world'\n")
    assert fields[0].default_value == "hello world"


def test_mismatched_quotes_are_left_alone() -> None:
    """Mixed/mismatched outer quotes are not silently reshaped."""

    fields = parse_env_example("VALUE=\"unterminated\n")
    assert fields[0].default_value == '"unterminated'


def test_empty_quoted_value_collapses_to_empty_string() -> None:
    fields = parse_env_example('NOTHING=""\n')
    assert fields[0].default_value == ""


def test_inner_quotes_inside_quoted_value_are_preserved() -> None:
    """Only the *outer* matching pair is stripped."""

    fields = parse_env_example("VALUE=\"a 'b' c\"\n")
    assert fields[0].default_value == "a 'b' c"


# ---------------------------------------------------------------------------
# Comment buffer (design §4.7 rules 2 + 3)
# ---------------------------------------------------------------------------


def test_single_comment_line_attaches_to_next_assignment() -> None:
    fields = parse_env_example("# Service port\nPORT=8082\n")
    assert fields[0].comment == "Service port"


def test_consecutive_comments_are_joined_with_newlines() -> None:
    """Adjacent ``#`` lines accumulate into one ``\\n``-joined block."""

    text = "# first line\n# second line\n# third line\nKEY=value\n"
    fields = parse_env_example(text)
    assert fields[0].comment == "first line\nsecond line\nthird line"


def test_blank_line_resets_comment_buffer() -> None:
    """Per design §4.7: a blank line discards any pending comment."""

    text = "# orphan comment\n\nKEY=value\n"
    fields = parse_env_example(text)
    assert fields[0].comment is None


def test_comment_after_assignment_attaches_to_next_assignment_only() -> None:
    """Each comment block belongs to exactly one assignment."""

    text = "# alpha\nA=1\n# bravo\nB=2\n"
    fields = parse_env_example(text)
    assert [(f.key, f.comment) for f in fields] == [("A", "alpha"), ("B", "bravo")]


def test_comment_buffer_does_not_leak_across_assignments() -> None:
    """A field with no preceding comment gets ``comment=None`` even when
    the *previous* field had a comment block."""

    text = "# alpha\nA=1\nB=2\n"
    fields = parse_env_example(text)
    assert fields[0].comment == "alpha"
    assert fields[1].comment is None


def test_comment_without_leading_space_is_preserved_verbatim() -> None:
    """``#hello`` → ``hello`` (no space to strip)."""

    fields = parse_env_example("#hello\nKEY=value\n")
    assert fields[0].comment == "hello"


def test_comment_strips_only_one_leading_space() -> None:
    """``#  indented`` keeps the second space — only one is consumed."""

    fields = parse_env_example("#  indented\nKEY=value\n")
    assert fields[0].comment == " indented"


def test_double_hash_comment_keeps_inner_hash() -> None:
    """``##`` is a comment whose body is ``#`` (no space-strip)."""

    fields = parse_env_example("##\nKEY=value\n")
    assert fields[0].comment == "#"


def test_section_header_style_comment() -> None:
    """The scaffold uses ``# === ... ===`` headers; preserve them faithfully."""

    text = "# ============================\n# admin-dashboard-api\n# ============================\nPORT=8082\n"
    fields = parse_env_example(text)
    assert fields[0].comment == "============================\nadmin-dashboard-api\n============================"


# ---------------------------------------------------------------------------
# Sensitivity derivation (Requirement 5.6 / Property C4 surface)
# ---------------------------------------------------------------------------


def test_sensitive_keys_are_flagged() -> None:
    """Keys matching the Sensitive_Env_Key suffixes carry ``is_sensitive=True``."""

    text = (
        "VAULT_TOKEN=dev-token-not-for-prod\n"
        "OPENAI_API_KEY=\n"
        "DB_PASSWORD=change-me\n"
        "POSTGRES_DSN=postgresql://x\n"
        "AWS_CREDENTIAL=foo\n"
        "SOME_PRIVATE_THING=bar\n"
    )
    fields = parse_env_example(text)
    flags = {f.key: f.is_sensitive for f in fields}
    assert flags == {
        "VAULT_TOKEN": True,
        "OPENAI_API_KEY": True,
        "DB_PASSWORD": True,
        "POSTGRES_DSN": True,
        "AWS_CREDENTIAL": True,
        "SOME_PRIVATE_THING": True,
    }


def test_non_sensitive_keys_are_not_flagged() -> None:
    """Common scaffold keys without sensitive suffixes stay non-sensitive."""

    text = (
        "PORT=8082\n"
        "LOG_LEVEL=INFO\n"
        "TEMPORAL_HOST=temporal:7233\n"
        "CLIENT_SOURCE=admin-dashboard-api\n"
    )
    fields = parse_env_example(text)
    assert all(not f.is_sensitive for f in fields)


# ---------------------------------------------------------------------------
# End-to-end realistic input
# ---------------------------------------------------------------------------


def test_realistic_env_example_mixes_all_rules() -> None:
    """Smoke-test against an input shaped like the real admin-dashboard-api
    ``.env.example`` (header banner, sectioned comments, blank stanza
    separators, sensitive + non-sensitive keys, quoted defaults)."""

    text = """\
# =============================================================================
# admin-dashboard-api .env.example
# =============================================================================

# --- Service ---
PORT=8082
LOG_LEVEL=INFO

# --- Vault ---
VAULT_ADDR=http://vault:8200
VAULT_TOKEN=dev-token-not-for-prod

# Greeting (quoted)
MESSAGE="hello world"
"""

    fields = parse_env_example(text)

    # Order matches the file.
    assert [f.key for f in fields] == [
        "PORT",
        "LOG_LEVEL",
        "VAULT_ADDR",
        "VAULT_TOKEN",
        "MESSAGE",
    ]

    # Section headers reset on blank lines, so PORT carries the
    # ``--- Service ---`` block but *not* the top banner.
    by_key = {f.key: f for f in fields}
    assert by_key["PORT"].comment == "--- Service ---"
    assert by_key["LOG_LEVEL"].comment is None  # buffer cleared by PORT
    assert by_key["VAULT_ADDR"].comment == "--- Vault ---"
    assert by_key["VAULT_TOKEN"].comment is None
    assert by_key["MESSAGE"].comment == "Greeting (quoted)"

    # Quote handling.
    assert by_key["MESSAGE"].default_value == "hello world"

    # Sensitivity wiring.
    assert by_key["VAULT_TOKEN"].is_sensitive is True
    assert by_key["VAULT_ADDR"].is_sensitive is False
    assert by_key["PORT"].is_sensitive is False


def test_lhs_set_matches_assignment_lines_only() -> None:
    """Property P4 surface — the LHS key set returned by the parser
    equals the set of assignment-shaped lines in the source text."""

    text = """\
# orphan
not an assignment
ALSO=ok
lower=ignored
123=ignored
mixed_CASE=ignored
GOOD=1
"""
    # Only the all-uppercase, letter-leading identifiers count.
    # ``lower=...``, ``123=...`` and ``mixed_CASE=...`` all fail the
    # ``^[A-Z][A-Z0-9_]*=.*$`` rule and are silently skipped.
    keys = {f.key for f in parse_env_example(text)}
    assert keys == {"ALSO", "GOOD"}


def test_crlf_line_endings_are_handled() -> None:
    """Windows-style ``\\r\\n`` newlines parse the same as ``\\n``."""

    text = "# alpha\r\nA=1\r\n\r\nB=2\r\n"
    fields = parse_env_example(text)
    assert [(f.key, f.comment) for f in fields] == [("A", "alpha"), ("B", None)]


def test_returned_fields_are_immutable_dataclasses() -> None:
    """``EnvField`` is frozen so callers can't mutate parsed entries."""

    import dataclasses

    fields = parse_env_example("KEY=value\n")
    assert dataclasses.is_dataclass(fields[0])

    with __import_pytest_raises():
        fields[0].key = "OTHER"  # type: ignore[misc]


def __import_pytest_raises():
    """Local helper so the immutability assertion stays inline-readable.

    Importing ``pytest`` at the top of the file is already done
    implicitly by the test runner; this small wrapper just narrows
    ``raises`` to ``FrozenInstanceError`` without polluting the module
    namespace with another top-level import.
    """

    import dataclasses

    import pytest

    return pytest.raises(dataclasses.FrozenInstanceError)
