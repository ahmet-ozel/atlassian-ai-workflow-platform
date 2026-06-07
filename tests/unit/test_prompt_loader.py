"""Unit tests for ``prompts.loader.PromptLoader``.

These tests pin the contract for the file-backed prompt loader:

1. ``load(name)`` resolves the prompt body from one of ``self._roots``
   and caches the result by logical name.
2. ``version(name)`` surfaces the short git hash captured at read
   time; falls back to ``"unknown"`` when git is unavailable or the
   file is untracked (fail-soft per design "Hot-reload graceful
   failure").
3. ``render(name, vars=...)`` substitutes :class:`PromptVars` fields
   into the template; collection fields render as deterministic
   comma-joined strings; unknown placeholders raise
   :class:`PromptTemplateError`.
4. ``poll_loop()`` is fail-soft - a broken read keeps the existing
   cache row.
5. Resolution walks ``self._roots`` in order; missing prompts raise
   :class:`PromptNotFoundError`.

"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from prompts import (
    PromptLoader,
    PromptNotFoundError,
    PromptTemplateError,
    PromptVars,
)
from prompts.loader import _UNKNOWN_GIT_HASH, _git_short_hash_for


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def prompt_root(tmp_path: Path) -> Path:
    """A bare directory holding ``<name>.md`` prompt files (no git)."""

    (tmp_path / "assistant_chat.md").write_text(
        "Hello {department_id}, repos={department_repos}, "
        "caps={capabilities}, lang={default_language}, bot={bot_username}.",
        encoding="utf-8",
    )
    (tmp_path / "untracked.md").write_text("static body", encoding="utf-8")
    return tmp_path


@pytest.fixture
def git_prompt_root(tmp_path: Path) -> Path:
    """A directory that *is* a git repo with one committed prompt.

    The fixture is skipped at runtime when ``git`` is missing on
    PATH; the test then exercises the fail-soft branch via
    :func:`_git_short_hash_for` directly.
    """

    if shutil.which("git") is None:
        pytest.skip("git binary not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = repo / "tracked.md"
    prompt.write_text("body v1", encoding="utf-8")

    # Minimal git config - no global settings leak in CI.
    env_init = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "tracked.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo,
        check=True,
        env={**env_init, "PATH": ""} if False else None,
    )
    return repo


def _vars(**overrides: object) -> PromptVars:
    """Construct a :class:`PromptVars` with sensible defaults."""

    base: dict[str, object] = {
        "department_id": "payment",
        "department_repos": ("payment-api", "payment-ui"),
        "capabilities": frozenset({"jira", "bitbucket"}),
        "default_language": "tr",
        "bot_username": "bot.payment",
    }
    base.update(overrides)
    return PromptVars(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_loader_requires_at_least_one_root() -> None:
    with pytest.raises(ValueError):
        PromptLoader(roots=())


def test_loader_accepts_path_objects(prompt_root: Path) -> None:
    """``roots`` should accept any iterable of path-likes."""

    loader = PromptLoader(roots=(prompt_root,))
    assert loader.load("assistant_chat").startswith("Hello {department_id}")


# ---------------------------------------------------------------------------
# load + cache + resolve
# ---------------------------------------------------------------------------


def test_load_returns_body_and_caches(prompt_root: Path) -> None:
    loader = PromptLoader(roots=(prompt_root,))

    first = loader.load("assistant_chat")
    second = loader.load("assistant_chat")

    assert first == second
    # The cache row exists after the first call.
    assert "assistant_chat" in loader._cache  # noqa: SLF001 - internal contract test


def test_load_walks_roots_in_order(tmp_path: Path) -> None:
    """Earlier roots shadow later ones."""

    high = tmp_path / "high"
    low = tmp_path / "low"
    high.mkdir()
    low.mkdir()
    (high / "shared.md").write_text("from-high", encoding="utf-8")
    (low / "shared.md").write_text("from-low", encoding="utf-8")

    loader = PromptLoader(roots=(high, low))
    assert loader.load("shared") == "from-high"


def test_load_supports_subdirectory_names(tmp_path: Path) -> None:
    """``"notifications/workflow_failed"`` resolves to ``<root>/notifications/workflow_failed.md``."""

    sub = tmp_path / "notifications"
    sub.mkdir()
    (sub / "workflow_failed.md").write_text("failed body", encoding="utf-8")

    loader = PromptLoader(roots=(tmp_path,))
    assert loader.load("notifications/workflow_failed") == "failed body"


def test_load_raises_prompt_not_found_for_missing_prompt(prompt_root: Path) -> None:
    loader = PromptLoader(roots=(prompt_root,))
    with pytest.raises(PromptNotFoundError):
        loader.load("does_not_exist")


# ---------------------------------------------------------------------------
# version / git short hash
# ---------------------------------------------------------------------------


def test_version_falls_back_to_unknown_when_not_under_git(prompt_root: Path) -> None:
    """A bare directory with no ``.git`` resolves to the fallback."""

    loader = PromptLoader(roots=(prompt_root,))
    loader.load("untracked")
    assert loader.version("untracked") == _UNKNOWN_GIT_HASH


def test_version_returns_short_hash_when_committed(git_prompt_root: Path) -> None:
    loader = PromptLoader(roots=(git_prompt_root,))
    loader.load("tracked")
    short = loader.version("tracked")
    # ``git log --pretty=%h`` returns 7+ hex chars on a fresh repo.
    assert short != _UNKNOWN_GIT_HASH
    assert len(short) >= 7
    assert all(c in "0123456789abcdef" for c in short)


def test_git_short_hash_for_returns_unknown_for_missing_path(tmp_path: Path) -> None:
    """``_git_short_hash_for`` is fail-soft for paths outside any working tree."""

    bogus = tmp_path / "definitely-not-tracked.md"
    bogus.write_text("body", encoding="utf-8")
    assert _git_short_hash_for(bogus) == _UNKNOWN_GIT_HASH


def test_version_raises_keyerror_before_load(prompt_root: Path) -> None:
    """``version`` requires a prior ``load`` (matches design pseudocode)."""

    loader = PromptLoader(roots=(prompt_root,))
    with pytest.raises(KeyError):
        loader.version("assistant_chat")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_substitutes_all_template_vars(prompt_root: Path) -> None:
    loader = PromptLoader(roots=(prompt_root,))
    rendered = loader.render("assistant_chat", vars=_vars())

    assert "Hello payment" in rendered
    # tuples render as comma-joined deterministic strings.
    assert "repos=payment-api, payment-ui" in rendered
    # frozensets render as sorted (deterministic) comma-joined strings.
    assert "caps=bitbucket, jira" in rendered
    assert "lang=tr" in rendered
    assert "bot=bot.payment" in rendered


def test_render_is_deterministic_for_frozensets(prompt_root: Path) -> None:
    """Sorted join makes rendering stable regardless of insertion order."""

    loader = PromptLoader(roots=(prompt_root,))
    a = loader.render(
        "assistant_chat",
        vars=_vars(capabilities=frozenset({"a", "b", "c"})),
    )
    b = loader.render(
        "assistant_chat",
        vars=_vars(capabilities=frozenset({"c", "b", "a"})),
    )
    assert a == b


def test_load_raises_prompt_template_error_on_unknown_placeholder(
    tmp_path: Path,
) -> None:
    """Boot-time validator rejects unknown placeholders.

    :meth:`PromptLoader._read` runs :func:`validate_template_format`
    on the body before caching, so an
    unknown placeholder fails the very first ``load`` call instead of
    waiting until ``render`` time. That is the design's
    *fail-fast-at-boot* contract.
    """

    (tmp_path / "broken.md").write_text(
        "dept={department_id}, mystery={unknown_var}", encoding="utf-8"
    )
    loader = PromptLoader(roots=(tmp_path,))

    with pytest.raises(PromptTemplateError) as excinfo:
        loader.load("broken")

    assert "unknown_var" in str(excinfo.value)


def test_load_raises_prompt_template_error_on_positional_placeholder(
    tmp_path: Path,
) -> None:
    """Positional ``{}`` placeholders are not in the PromptVars contract."""

    (tmp_path / "positional.md").write_text("hello {0}", encoding="utf-8")
    loader = PromptLoader(roots=(tmp_path,))

    with pytest.raises(PromptTemplateError):
        loader.load("positional")


def test_render_supports_escaped_curly_braces(tmp_path: Path) -> None:
    """``{{`` / ``}}`` survive untouched (used for JSON examples in prompts)."""

    (tmp_path / "json_example.md").write_text(
        "use {{key: value}} for {department_id}", encoding="utf-8"
    )
    loader = PromptLoader(roots=(tmp_path,))

    rendered = loader.render("json_example", vars=_vars(department_id="payment"))
    assert rendered == "use {key: value} for payment"


# ---------------------------------------------------------------------------
# poll_loop
# ---------------------------------------------------------------------------


def test_poll_loop_refreshes_cache_when_mtime_advances(prompt_root: Path) -> None:
    """One poll iteration picks up an mtime increase and replaces the row."""

    loader = PromptLoader(roots=(prompt_root,), poll_interval_s=0)
    loader.load("untracked")
    initial = loader._cache["untracked"]  # noqa: SLF001 - internal contract

    # Mutate the file and bump mtime explicitly so the test does not
    # depend on filesystem timestamp resolution.
    target = prompt_root / "untracked.md"
    target.write_text("static body v2", encoding="utf-8")
    bumped_mtime = initial.mtime + 10.0
    import os

    os.utime(target, (bumped_mtime, bumped_mtime))

    async def _one_iteration() -> None:
        # Drive the poll loop just long enough for one pass.
        task = asyncio.create_task(loader.poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_one_iteration())

    refreshed = loader._cache["untracked"]  # noqa: SLF001
    assert refreshed.body == "static body v2"
    assert refreshed.mtime >= bumped_mtime


def test_poll_loop_keeps_cache_when_read_fails(prompt_root: Path) -> None:
    """Fail-soft: a broken read keeps the existing cache row."""

    loader = PromptLoader(roots=(prompt_root,), poll_interval_s=0)
    loader.load("untracked")
    cached_before = loader._cache["untracked"]  # noqa: SLF001

    # Delete the file so ``_read`` raises FileNotFoundError mid-poll.
    (prompt_root / "untracked.md").unlink()

    async def _one_iteration() -> None:
        task = asyncio.create_task(loader.poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_one_iteration())

    cached_after = loader._cache["untracked"]  # noqa: SLF001
    assert cached_after is cached_before
    assert cached_after.body == "static body"
