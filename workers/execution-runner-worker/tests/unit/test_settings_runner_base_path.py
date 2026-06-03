"""Unit tests for :class:`src.config.Settings.runner_base_path`.

``RUNNER_BASE_PATH`` is the canonical env var; ``SSH_BASE_PATH`` is preserved
as a deprecated alias for backwards compatibility. The Hypothesis-based
property test that randomises the full input space lives in
``platform/tests/property/test_runner_workspace_path.py``; this file
documents the alias resolution priority at the example level so a developer
can ``pytest tests/unit -k runner_base_path`` for fast feedback.
"""

from __future__ import annotations

import pytest

from src.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the env vars the alias chain reads so each test starts clean."""

    monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
    monkeypatch.delenv("SSH_BASE_PATH", raising=False)


def _build(**overrides: str) -> Settings:
    """Construct ``Settings`` with ``.env`` loading disabled.

    Process env vars (set by the calling test via monkeypatch) are still
    honoured; only the per-worker ``.env`` file is bypassed so the test
    is hermetic regardless of the developer's local working tree.
    """

    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Default + alias resolution
# ---------------------------------------------------------------------------


class TestRunnerBasePathAliasResolution:
    """Exercises the ``RUNNER_BASE_PATH > SSH_BASE_PATH > default`` chain."""

    def test_default_when_neither_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)

        settings = _build()

        assert settings.runner_base_path == "/var/ai-runner"

    def test_ssh_base_path_used_when_only_legacy_alias_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("SSH_BASE_PATH", "/legacy/ai-runner")

        settings = _build()

        assert settings.runner_base_path == "/legacy/ai-runner"

    def test_runner_base_path_used_when_only_canonical_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("RUNNER_BASE_PATH", "/srv/runner")

        settings = _build()

        assert settings.runner_base_path == "/srv/runner"

    def test_canonical_wins_over_legacy_alias_when_both_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AliasChoices semantics: the first declared name wins. The
        # Settings field declares ``RUNNER_BASE_PATH`` ahead of
        # ``SSH_BASE_PATH``, so the canonical name takes precedence even
        # when the deprecated alias is also populated. This is the
        # backwards-compatibility contract.
        _clear_env(monkeypatch)
        monkeypatch.setenv("SSH_BASE_PATH", "/legacy/ai-runner")
        monkeypatch.setenv("RUNNER_BASE_PATH", "/srv/runner")

        settings = _build()

        assert settings.runner_base_path == "/srv/runner"

    def test_case_insensitive_canonical_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SettingsConfigDict(case_sensitive=False) — confirm the lower-
        # case form is also recognised so misconfigured shells don't
        # silently fall through to the default.
        _clear_env(monkeypatch)
        monkeypatch.setenv("runner_base_path", "/lowercase/runner")

        settings = _build()

        assert settings.runner_base_path == "/lowercase/runner"
