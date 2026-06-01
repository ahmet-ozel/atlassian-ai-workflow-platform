"""Unit tests for ``prompts.validate.validate_template_format``.

Validates: Requirements 2.9
"""

from __future__ import annotations

import pytest

from prompts.validate import (
    KNOWN_TEMPLATE_VARS,
    PromptTemplateError,
    validate_template_format,
)


class TestAcceptsValidTemplates:
    def test_empty_body(self) -> None:
        validate_template_format("")

    def test_plain_markdown_no_placeholders(self) -> None:
        validate_template_format("# Heading\n\nJust plain text without braces.")

    def test_all_known_placeholders(self) -> None:
        body = (
            "Dept: {department_id}\n"
            "Repos: {department_repos}\n"
            "Caps: {capabilities}\n"
            "Lang: {default_language}\n"
            "Bot: {bot_username}\n"
        )
        validate_template_format(body)

    def test_repeated_placeholder(self) -> None:
        body = "{department_id} and {department_id} again"
        validate_template_format(body)

    def test_escaped_literal_braces(self) -> None:
        body = "JSON example: {{\"key\": \"value\"}} for {department_id}."
        validate_template_format(body)

    def test_escaped_braces_only(self) -> None:
        body = "Literal {{ and }} with no real placeholders."
        validate_template_format(body)

    def test_known_placeholder_with_format_spec(self) -> None:
        # ``str.format`` allows format-specs like ``{name:>10}`` —
        # validator should accept them when the root is known.
        body = "Padded: [{department_id:>10}]"
        validate_template_format(body)

    def test_known_placeholder_with_conversion(self) -> None:
        body = "Repr: {department_id!r}"
        validate_template_format(body)


class TestRejectsUnbalancedBraces:
    def test_single_open_brace(self) -> None:
        with pytest.raises(PromptTemplateError):
            validate_template_format("hello {")

    def test_single_close_brace(self) -> None:
        with pytest.raises(PromptTemplateError):
            validate_template_format("hello }")

    def test_open_brace_without_closing(self) -> None:
        with pytest.raises(PromptTemplateError):
            validate_template_format("text {department_id and more")

    def test_lone_close_brace_in_text(self) -> None:
        with pytest.raises(PromptTemplateError):
            validate_template_format("text } more")


class TestRejectsUnknownPlaceholders:
    def test_typo_in_known_var(self) -> None:
        with pytest.raises(PromptTemplateError) as excinfo:
            validate_template_format("Hello {departement_id}")
        assert "unknown placeholder" in str(excinfo.value)
        assert "departement_id" in str(excinfo.value)

    def test_completely_unknown_var(self) -> None:
        with pytest.raises(PromptTemplateError) as excinfo:
            validate_template_format("foo {user_email} bar")
        assert "unknown placeholder" in str(excinfo.value)

    def test_unknown_placeholder_among_known(self) -> None:
        body = "{department_id} {capabilities} {extra_var}"
        with pytest.raises(PromptTemplateError):
            validate_template_format(body)


class TestRejectsPositionalPlaceholders:
    def test_auto_numbered_placeholder(self) -> None:
        with pytest.raises(PromptTemplateError):
            validate_template_format("hello {} world")

    def test_indexed_positional_placeholder(self) -> None:
        with pytest.raises(PromptTemplateError):
            validate_template_format("hello {0} world")


class TestKnownVarsContract:
    def test_known_vars_set_matches_requirement(self) -> None:
        # R2.7 lists exactly these five names; guard against drift.
        assert KNOWN_TEMPLATE_VARS == frozenset(
            {
                "department_id",
                "department_repos",
                "capabilities",
                "default_language",
                "bot_username",
            }
        )


class TestPromptTemplateErrorIsValueError:
    def test_subclass_relationship(self) -> None:
        # Call sites occasionally treat template failures as generic
        # configuration ``ValueError``s; verify the contract.
        assert issubclass(PromptTemplateError, ValueError)
