"""Unit tests for ``redact_secrets`` (Requirements 44.2, 44.5)."""

from __future__ import annotations

import copy

import pytest

from mcp_atlassian.utils.secret_redaction import (
    DEFAULT_SECRET_KEYS,
    REDACTED_PLACEHOLDER,
    redact_secrets,
)


class TestRedactSecretsFlat:
    """Flat-dict redaction for canonical secret keys."""

    def test_flat_dict_with_secret_key_redacted(self):
        assert redact_secrets({"token": "abc"}) == {"token": REDACTED_PLACEHOLDER}

    def test_flat_dict_without_secret_key_preserved(self):
        assert redact_secrets({"name": "widget"}) == {"name": "widget"}

    @pytest.mark.parametrize("key", sorted(DEFAULT_SECRET_KEYS))
    def test_every_default_key_is_redacted(self, key: str):
        """Each member of ``DEFAULT_SECRET_KEYS`` triggers redaction."""
        assert redact_secrets({key: "sensitive"}) == {key: REDACTED_PLACEHOLDER}


class TestRedactSecretsNested:
    """Recursive redaction for nested containers (Requirement 44.2/44.5)."""

    def test_nested_dict(self):
        assert redact_secrets({"outer": {"password": "p"}}) == {
            "outer": {"password": REDACTED_PLACEHOLDER}
        }

    def test_deeply_nested_dict(self):
        obj = {"a": {"b": {"c": {"token": "deep"}}}}
        assert redact_secrets(obj) == {
            "a": {"b": {"c": {"token": REDACTED_PLACEHOLDER}}}
        }

    def test_list_of_dicts(self):
        assert redact_secrets([{"apiKey": "k"}, {"other": "v"}]) == [
            {"apiKey": REDACTED_PLACEHOLDER},
            {"other": "v"},
        ]

    def test_tuple_preserved_as_tuple(self):
        result = redact_secrets(({"secret": "s"},))
        assert isinstance(result, tuple)
        assert result == ({"secret": REDACTED_PLACEHOLDER},)

    def test_mixed_container_structure(self):
        obj = {
            "users": [
                {"name": "alice", "password": "p1"},
                {"name": "bob", "tokens": ({"token": "t"},)},
            ],
            "clientSecret": "cs",
        }
        expected = {
            "users": [
                {"name": "alice", "password": REDACTED_PLACEHOLDER},
                {"name": "bob", "tokens": ({"token": REDACTED_PLACEHOLDER},)},
            ],
            "clientSecret": REDACTED_PLACEHOLDER,
        }
        assert redact_secrets(obj) == expected


class TestRedactSecretsCaseInsensitive:
    """Case-insensitive matching on the final key name only (Requirement 44.5)."""

    @pytest.mark.parametrize(
        "key",
        ["Secret", "SECRET", "clientSecret", "CLIENT_SECRET", "Token", "PASSWORD"],
    )
    def test_case_variations_redacted(self, key: str):
        assert redact_secrets({key: "x"}) == {key: REDACTED_PLACEHOLDER}

    @pytest.mark.parametrize(
        "key",
        [
            "description",  # contains no secret keyword
            "tokenize",  # substring "token" but not an exact match
            "mysecretvalue",  # substring "secret" but not an exact match
            "password_hint",  # extends past "password"
            "access",  # unrelated word
        ],
    )
    def test_non_matching_keys_preserved(self, key: str):
        assert redact_secrets({key: "value"}) == {key: "value"}


class TestRedactSecretsNonStringKeys:
    """Non-string keys are never matched and values recurse normally."""

    def test_numeric_key_left_alone(self):
        assert redact_secrets({1: "keep", 2: "me"}) == {1: "keep", 2: "me"}

    def test_numeric_key_with_nested_secret_still_recurses(self):
        obj = {1: {"token": "abc"}}
        assert redact_secrets(obj) == {1: {"token": REDACTED_PLACEHOLDER}}


class TestRedactSecretsPrimitives:
    """Primitives pass through unchanged."""

    @pytest.mark.parametrize(
        "value", ["hello", 42, 3.14, None, True, False, b"bytes"]
    )
    def test_primitive_passthrough(self, value):
        assert redact_secrets(value) == value

    def test_empty_dict(self):
        assert redact_secrets({}) == {}

    def test_empty_list(self):
        assert redact_secrets([]) == []

    def test_empty_tuple(self):
        result = redact_secrets(())
        assert isinstance(result, tuple)
        assert result == ()


class TestRedactSecretsCustomKeys:
    """Custom ``keys`` parameter overrides the default set."""

    def test_custom_keys_redacts_named_field(self):
        result = redact_secrets({"foo": 1}, keys=frozenset({"foo"}))
        assert result == {"foo": REDACTED_PLACEHOLDER}

    def test_custom_keys_does_not_redact_defaults(self):
        """When callers supply their own set, the defaults do not apply."""
        result = redact_secrets({"token": "abc"}, keys=frozenset({"foo"}))
        assert result == {"token": "abc"}

    def test_custom_keys_case_insensitive(self):
        result = redact_secrets({"FOO": 1}, keys=frozenset({"foo"}))
        assert result == {"FOO": REDACTED_PLACEHOLDER}

    def test_custom_keys_empty_redacts_nothing(self):
        obj = {"token": "abc", "password": "p"}
        assert redact_secrets(obj, keys=frozenset()) == obj


class TestRedactSecretsImmutability:
    """The original input must not be mutated."""

    def test_flat_dict_not_mutated(self):
        original = {"token": "abc", "name": "widget"}
        snapshot = copy.deepcopy(original)
        redact_secrets(original)
        assert original == snapshot

    def test_nested_structure_not_mutated(self):
        original = {
            "users": [
                {"name": "alice", "password": "p1"},
                {"name": "bob", "clientSecret": "cs"},
            ],
            "meta": ({"apiKey": "k"},),
        }
        snapshot = copy.deepcopy(original)
        redact_secrets(original)
        assert original == snapshot

    def test_returned_dict_is_new_object(self):
        original = {"token": "abc"}
        result = redact_secrets(original)
        assert result is not original

    def test_returned_list_is_new_object(self):
        original = [{"token": "abc"}]
        result = redact_secrets(original)
        assert result is not original
