"""Invariant test: Description override validation.

**: Description override validation
--------------------------------------------
*For any* invalid value supplied to a recognised YAML front-matter
field (``workflow_type``, ``cleanup``, ``timeout_seconds``, the
boolean flags, or entries / top-level shape of the ``output`` list),
the:func:`parse_description_frontmatter` activity SHALL drop that
field to ``None`` and record a corresponding entry in
``parse_errors``, while preserving the values of every sibling field
that was supplied with a *valid* value.

Strategy
--------
For each recognised field the test draws one of three branches:

 0. omit the field entirely
 1. supply a *valid* value drawn from the allowed vocabulary
 2. supply an *invalid* value drawn from a complementary strategy

The chosen ``ai-bot`` mapping is then serialised through
``yaml.safe_dump`` so the input to the parser is always well-formed
YAML - only the **field-level** values are invalid. After parsing we
assert the post-conditions field:

* valid  ``result.<field>`` equals what we supplied AND the field
 name does not appear in any ``parse_errors`` entry.
* invalid  ``result.<field>`` is ``None`` AND at least one
 ``parse_errors`` entry mentions the field name.

The ``output`` field is exercised at the *top level* (a non-list
shape forces the whole field to ``None`` with a single error). The
unit-test suite already covers per-entry validation; the Invariant test focuses on the field-level invariant required by.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - match the convention used by sibling Invariant tests.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities.description_parser import (  # noqa: E402
    TIMEOUT_SECONDS_MAX,
    TIMEOUT_SECONDS_MIN,
    VALID_CLEANUP_POLICIES,
    VALID_WORKFLOW_TYPES,
    parse_description_frontmatter,
)


# ---------------------------------------------------------------------------
# Constants kept in sync with the parser's private vocabularies. We do
# not import the private symbol so the test stays decoupled from
# implementation details.
# ---------------------------------------------------------------------------

_VALID_OUTPUT_TYPES: tuple[str, ...] = (
    "jira_comment",
    "jira_attachment",
    "bitbucket_commit",
    "bitbucket_create_pr",
    "confluence_create_page",
    "confluence_update_page",
    "jira_transition",
)


# Identifier-shaped strings - chosen to round-trip through
# ``yaml.safe_dump`` / ``yaml.safe_load`` without surprises (no
# leading dashes, no whitespace, no characters that need quoting).
# A minimum length of 2 keeps the value past the parser's
# ``allow_empty=False`` guard.
_safe_string: st.SearchStrategy[str] = st.from_regex(
    r"[A-Za-z][A-Za-z0-9_\-]{1,30}", fullmatch=True
)


# ---------------------------------------------------------------------------
# Per-field strategies - valid and invalid branches
# ---------------------------------------------------------------------------


def _valid_workflow_type() -> st.SearchStrategy[str]:
    return st.sampled_from(sorted(VALID_WORKFLOW_TYPES))


def _invalid_workflow_type() -> st.SearchStrategy[Any]:
    return st.one_of(
        # An identifier that is *not* in the closed set.
        _safe_string.filter(lambda s: s not in VALID_WORKFLOW_TYPES),
        # Wrong-typed scalars - non-string is rejected outright.
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.lists(st.integers(), max_size=3),
    )


def _valid_cleanup() -> st.SearchStrategy[str]:
    return st.sampled_from(sorted(VALID_CLEANUP_POLICIES))


def _invalid_cleanup() -> st.SearchStrategy[Any]:
    return st.one_of(
        _safe_string.filter(lambda s: s not in VALID_CLEANUP_POLICIES),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.lists(st.integers(), max_size=3),
    )


def _valid_timeout() -> st.SearchStrategy[int]:
    return st.integers(
        min_value=TIMEOUT_SECONDS_MIN, max_value=TIMEOUT_SECONDS_MAX
    )


def _invalid_timeout() -> st.SearchStrategy[Any]:
    return st.one_of(
        st.integers(max_value=TIMEOUT_SECONDS_MIN - 1),
        st.integers(min_value=TIMEOUT_SECONDS_MAX + 1),
        _safe_string,
        st.floats(allow_nan=False, allow_infinity=False),
        # Booleans subclass int in Python; the parser must still reject
        # them.
        st.booleans(),
    )


def _valid_bool() -> st.SearchStrategy[bool]:
    return st.booleans()


def _invalid_bool() -> st.SearchStrategy[Any]:
    return st.one_of(
        _safe_string,
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.lists(st.integers(), max_size=3),
    )


def _valid_repo_or_branch() -> st.SearchStrategy[str]:
    return _safe_string


def _invalid_repo_or_branch() -> st.SearchStrategy[Any]:
    # Any non-string scalar / collection. Empty strings are intentionally
    # excluded - they are also rejected by the parser, but the property
    # under test focuses on type-level invalidity.
    return st.one_of(
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(_safe_string, st.integers(), max_size=2),
    )


def _valid_output_entry() -> st.SearchStrategy[dict[str, Any]]:
    """A well-formed action mapping: ``{type, params}``."""
    return st.fixed_dictionaries({
        "type": st.sampled_from(_VALID_OUTPUT_TYPES),
        "params": st.dictionaries(
            _safe_string, _safe_string, max_size=3
        ),
    })


def _valid_output() -> st.SearchStrategy[list[dict[str, Any]]]:
    return st.lists(_valid_output_entry(), max_size=4)


def _invalid_output_top_level() -> st.SearchStrategy[Any]:
    """Top-level shape that is not a list - drops the entire field."""
    return st.one_of(
        _safe_string,
        st.integers(),
        st.dictionaries(_safe_string, _safe_string, max_size=2),
    )


# ---------------------------------------------------------------------------
# Composite: build a (yaml_dict, expectations) pair
# ---------------------------------------------------------------------------


_FIELD_SPECS: tuple[
    tuple[str, st.SearchStrategy[Any], st.SearchStrategy[Any]], ...
] = (
    ("workflow_type", _valid_workflow_type(), _invalid_workflow_type()),
    ("repo", _valid_repo_or_branch(), _invalid_repo_or_branch()),
    ("branch", _valid_repo_or_branch(), _invalid_repo_or_branch()),
    ("needs_ssh", _valid_bool(), _invalid_bool()),
    ("needs_docker", _valid_bool(), _invalid_bool()),
    ("cleanup", _valid_cleanup(), _invalid_cleanup()),
    ("timeout_seconds", _valid_timeout(), _invalid_timeout()),
    ("web_search", _valid_bool(), _invalid_bool()),
)


@st.composite
def _override_block(
    draw: st.DrawFn,
) -> tuple[dict[str, Any], dict[str, tuple[str, Any]]]:
    """Generate an ``ai-bot`` mapping and the per-field verdicts.

 Returns ``(ai_bot, expectations)`` where ``expectations`` maps each
 *included* field name to ``("valid", expected_value)`` or
 ``("invalid", None)``. Omitted fields do not appear in
 ``expectations``.
 """
    ai_bot: dict[str, Any] = {}
    expectations: dict[str, tuple[str, Any]] = {}

    for name, valid_strat, invalid_strat in _FIELD_SPECS:
        # 0 = omit, 1 = valid, 2 = invalid
        choice = draw(st.integers(min_value=0, max_value=2))
        if choice == 0:
            continue
        if choice == 1:
            value = draw(valid_strat)
            ai_bot[name] = value
            expectations[name] = ("valid", value)
        else:
            value = draw(invalid_strat)
            ai_bot[name] = value
            expectations[name] = ("invalid", None)

    # ``output`` - handled separately because its valid shape (list of
    # dicts) and invalid shape (non-list) require different generators.
    output_choice = draw(st.integers(min_value=0, max_value=2))
    if output_choice == 1:
        entries = draw(_valid_output())
        ai_bot["output"] = entries
        # The parser normalises every entry to ``{"type":..., "params":...}``
        # and wraps a missing ``params`` in an empty dict. Our valid
        # generator always supplies ``params`` so the round-trip is a
        # straightforward shallow copy.
        expectations["output"] = (
            "valid",
            [
                {"type": e["type"], "params": dict(e["params"])}
                for e in entries
            ],
        )
    elif output_choice == 2:
        ai_bot["output"] = draw(_invalid_output_top_level())
        expectations["output"] = ("invalid", None)

    return ai_bot, expectations


# ---------------------------------------------------------------------------
# YAML rendering helper
# ---------------------------------------------------------------------------


def _render_description(ai_bot: dict[str, Any]) -> str:
    """Wrap an ``ai-bot`` mapping in the ``---``-delimited front-matter."""
    body = yaml.safe_dump(
        {"ai-bot": ai_bot},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    if not body.endswith("\n"):
        body += "\n"
    return f"---\n{body}---\n\n## Body\nIrrelevant prose.\n"


def _error_blames_field(error: str, field_name: str) -> bool:
    """Best-effort match: every ``parse_errors`` entry leads with the field
 name, so a substring check is sufficient and stays decoupled from
 the exact wording the parser uses.
 """
    return field_name in error


# ---------------------------------------------------------------------------
# invariant
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(_override_block())
def test_invalid_yaml_field_dropped_with_warning_entry(
    block: tuple[dict[str, Any], dict[str, tuple[str, Any]]],
) -> None:
    """: invalid YAML field  ``None`` + ``parse_errors``
 entry, while valid sibling fields are preserved verbatim.

 **"""
    ai_bot, expectations = block

    description = _render_description(ai_bot)
    result = parse_description_frontmatter(description)

    assert result is not None, (
        "Parser returned None for a well-formed front-matter block; "
        f"description={description!r}"
    )

    for field_name, (verdict, expected) in expectations.items():
        actual = getattr(result, field_name)

        if verdict == "valid":
            assert actual == expected, (
                f"Valid field {field_name!r} was not preserved. "
                f"Supplied {expected!r}, got {actual!r}. "
                f"parse_errors={result.parse_errors}"
            )
            assert not any(
                _error_blames_field(err, field_name)
                for err in result.parse_errors
            ), (
                f"Valid field {field_name!r} unexpectedly produced a "
                f"parse_errors entry: {result.parse_errors}"
            )
        else:
            assert actual is None, (
                f"Invalid field {field_name!r} should have been dropped to "
                f"None, got {actual!r}. parse_errors={result.parse_errors}"
            )
            assert any(
                _error_blames_field(err, field_name)
                for err in result.parse_errors
            ), (
                f"Invalid field {field_name!r} did not produce a "
                f"parse_errors entry. parse_errors={result.parse_errors}"
            )

    # Aggregate sanity: every parse_errors entry must blame *some*
    # recognised field name. This guards against accidental noise (the
    # parser logging unrelated diagnostics into ``parse_errors``).
    all_field_names = {name for name, _, _ in _FIELD_SPECS} | {"output"}
    for err in result.parse_errors:
        assert any(name in err for name in all_field_names), (
            f"parse_errors entry {err!r} does not name any recognised "
            f"field; expected one of {sorted(all_field_names)}"
        )
