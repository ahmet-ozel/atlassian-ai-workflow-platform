"""Unit tests for ``prompts.types`` - :class:`PromptVars` + helper.

These tests pin the contract for the prompt rendering layer:

1. :class:`PromptVars` ships exactly the five mandatory fields
   (``department_id``, ``department_repos``,
   ``capabilities``, ``default_language``, ``bot_username``) and is
   frozen + slotted.
2. The collection fields use immutable types (``tuple``,
   ``frozenset``) so the whole value is hashable.
3. ``inject_template_vars`` substitutes every placeholder with the
   matching attribute and propagates ``KeyError`` when a body
   references an unknown placeholder (the loader catches
   this and converts it to ``PromptTemplateError``).
4. ``TEMPLATE_VARIABLE_NAMES`` mirrors the dataclass' field set so
   ``validate_template_format`` shares
   one source of truth with the renderer.

"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import get_args, get_type_hints

import pytest

from prompts import (
    TEMPLATE_VARIABLE_NAMES,
    PromptVars,
    inject_template_vars,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vars(
    *,
    department_id: str = "payment",
    department_repos: tuple[str, ...] = ("payment-api", "payment-ui"),
    capabilities: frozenset[str] = frozenset({"jira", "bitbucket"}),
    default_language: str = "tr",
    bot_username: str = "bot.payment",
) -> PromptVars:
    """Build a fully-populated :class:`PromptVars` for tests."""

    return PromptVars(
        department_id=department_id,
        department_repos=department_repos,
        capabilities=capabilities,  # type: ignore[arg-type]
        default_language=default_language,  # type: ignore[arg-type]
        bot_username=bot_username,
    )


# ---------------------------------------------------------------------------
# PromptVars shape
# ---------------------------------------------------------------------------


def test_prompt_vars_is_frozen_dataclass() -> None:
    """``PromptVars`` must be a frozen dataclass."""

    assert is_dataclass(PromptVars)
    assert PromptVars.__dataclass_params__.frozen is True


def test_prompt_vars_uses_slots() -> None:
    """Slots keep per-render allocation cheap on the chat hot path."""

    # ``__slots__`` is defined when ``slots=True`` is passed to
    # ``@dataclass``; checking the attribute is the most direct proof.
    assert hasattr(PromptVars, "__slots__")


def test_prompt_vars_field_set_matches_requirement() -> None:
    """The five mandatory fields are present, no more and no fewer."""

    expected = {
        "department_id",
        "department_repos",
        "capabilities",
        "default_language",
        "bot_username",
    }
    assert {f.name for f in fields(PromptVars)} == expected


def test_template_variable_names_mirrors_field_set() -> None:
    """``TEMPLATE_VARIABLE_NAMES`` is the single source of truth."""

    assert TEMPLATE_VARIABLE_NAMES == frozenset(f.name for f in fields(PromptVars))


def test_prompt_vars_is_immutable() -> None:
    """Reassigning a field on a frozen dataclass must raise."""

    vars_ = _make_vars()
    with pytest.raises(FrozenInstanceError):
        vars_.department_id = "other"  # type: ignore[misc]


def test_prompt_vars_is_hashable() -> None:
    """All collection fields use immutable types, so the value is hashable."""

    vars_ = _make_vars()
    assert hash(vars_) == hash(_make_vars())


def test_default_language_literal_is_tr_or_en() -> None:
    """The language vocabulary is pinned to ``{"tr", "en"}``."""

    hints = get_type_hints(PromptVars)
    assert set(get_args(hints["default_language"])) == {"tr", "en"}


# ---------------------------------------------------------------------------
# inject_template_vars
# ---------------------------------------------------------------------------


def test_inject_template_vars_substitutes_all_placeholders() -> None:
    """Every ``{<field>}`` placeholder is replaced by the attribute value."""

    body = (
        "dept={department_id} repos={department_repos} "
        "caps={capabilities} lang={default_language} bot={bot_username}"
    )
    vars_ = _make_vars(
        department_id="payment",
        department_repos=("payment-api",),
        capabilities=frozenset({"jira"}),
        default_language="en",
        bot_username="bot.payment",
    )

    rendered = inject_template_vars(body, vars_)

    assert "dept=payment" in rendered
    assert "repos=('payment-api',)" in rendered
    assert "caps=frozenset({'jira'})" in rendered
    assert "lang=en" in rendered
    assert "bot=bot.payment" in rendered


def test_inject_template_vars_is_no_op_without_placeholders() -> None:
    """A body without placeholders is returned unchanged."""

    body = "no placeholders here"
    assert inject_template_vars(body, _make_vars()) == body


def test_inject_template_vars_supports_curly_brace_escape() -> None:
    """``{{`` / ``}}`` literals survive the format pass intact.

    ``validate_template_format`` rejects unbalanced
    single braces, but escaped braces must round-trip cleanly so
    prompt authors can show JSON examples to the LLM.
    """

    body = "use {{json}} for example, dept={department_id}"
    rendered = inject_template_vars(body, _make_vars(department_id="payment"))
    assert rendered == "use {json} for example, dept=payment"


def test_inject_template_vars_raises_keyerror_on_unknown_placeholder() -> None:
    """An unknown placeholder propagates ``KeyError`` to the loader.

    ``PromptLoader.render`` catches this and converts it to
    ``PromptTemplateError`` so the CI gate fails fast.
    """

    body = "dept={department_id}, mystery={unknown_var}"
    with pytest.raises(KeyError):
        inject_template_vars(body, _make_vars())
