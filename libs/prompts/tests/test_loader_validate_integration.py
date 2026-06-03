"""Integration test: ``PromptLoader._read`` runs ``validate_template_format``.

The loader must reject malformed prompt
bodies *at read time* — i.e. ``load`` (and the hot-reload
``poll_loop``) surfaces a :class:`PromptTemplateError` instead of
caching the bad body and surfacing a confusing ``KeyError`` later
during ``str.format`` rendering. This test pins that wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prompts import PromptLoader, PromptTemplateError


def _write_prompt(root: Path, name: str, body: str) -> None:
    target = root / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


class TestLoaderRunsValidator:
    def test_valid_body_loads(self, tmp_path: Path) -> None:
        _write_prompt(
            tmp_path,
            "ok_prompt",
            "Hello {department_id}, repos: {department_repos}.",
        )
        loader = PromptLoader(roots=(tmp_path,))
        assert "department_id" in loader.load("ok_prompt")

    def test_unknown_placeholder_rejected_at_load(self, tmp_path: Path) -> None:
        _write_prompt(
            tmp_path,
            "bad_unknown",
            "Hello {department_id}, but also {not_a_var}.",
        )
        loader = PromptLoader(roots=(tmp_path,))
        with pytest.raises(PromptTemplateError) as excinfo:
            loader.load("bad_unknown")
        assert "unknown placeholder" in str(excinfo.value)

    def test_unbalanced_brace_rejected_at_load(self, tmp_path: Path) -> None:
        _write_prompt(
            tmp_path,
            "bad_brace",
            "JSON-ish: { not escaped",
        )
        loader = PromptLoader(roots=(tmp_path,))
        with pytest.raises(PromptTemplateError):
            loader.load("bad_brace")

    def test_escaped_braces_pass(self, tmp_path: Path) -> None:
        _write_prompt(
            tmp_path,
            "json_example",
            'Example: {{"id": "{department_id}"}}',
        )
        loader = PromptLoader(roots=(tmp_path,))
        # No exception should be raised.
        body = loader.load("json_example")
        assert "{department_id}" in body
