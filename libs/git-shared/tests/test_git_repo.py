"""Unit tests for ``git_shared.GitRepo``.

These tests exercise the full mutation flow against a real on-disk
repository created in a ``tmp_path`` so the ``IndexFile`` /
``hash-object`` paths are verified end-to-end. GitPython is a
hard dependency; a missing install would fail at the
``import git_shared`` step and surface a clear traceback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make ``git_shared`` importable when pytest is run from the lib root
# without prior ``pip install -e .``.
_LIB_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_LIB_SRC) not in sys.path:
    sys.path.insert(0, str(_LIB_SRC))

import git  # noqa: E402

from git_shared import (  # noqa: E402
    BranchAlreadyExistsError,
    BranchNotFoundError,
    GitAuthor,
    GitRepo,
    GitRepoError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """Initialise a fresh git repo with a single seeded ``main`` commit.

    Returns the absolute path so the test body can pass it to
    :class:`GitRepo`.
    """

    target = tmp_path / "repo"
    target.mkdir()
    repo = git.Repo.init(str(target), initial_branch="main")
    # Configure a deterministic identity so the seed commit succeeds
    # on hosts without a global ``user.email`` configured. Disable
    # autocrlf so the test assertions hold regardless of host OS
    # (Windows CRLF would otherwise round-trip to LF on read).
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Seed Author")
        cfg.set_value("user", "email", "seed@example.com")
        cfg.set_value("core", "autocrlf", "false")

    seed = target / "prompts" / "seed.md"
    seed.parent.mkdir(parents=True)
    seed.write_bytes(b"seed body\n")
    repo.index.add([str(seed)])
    repo.index.commit("seed")
    return target


@pytest.fixture
def author() -> GitAuthor:
    return GitAuthor(name="Test Bot", email="bot@example.com")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_non_repository(self, tmp_path: Path) -> None:
        with pytest.raises(GitRepoError):
            GitRepo(repo_path=tmp_path)

    def test_repo_path_property(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        assert gr.repo_path == repo_path.resolve()
        assert gr.main_branch == "main"


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_reads_seeded_file(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        assert gr.read_file("prompts/seed.md") == "seed body\n"

    def test_missing_path_raises(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        with pytest.raises(FileNotFoundError):
            gr.read_file("does-not-exist.md")

    def test_unknown_branch_raises(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        with pytest.raises(BranchNotFoundError):
            gr.read_file("prompts/seed.md", branch="ghost")


class TestListFiles:
    def test_lists_default_md(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        assert gr.list_files() == ["prompts/seed.md"]

    def test_filters_by_prefix(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        assert gr.list_files(path_prefix="prompts/") == ["prompts/seed.md"]
        assert gr.list_files(path_prefix="other/") == []

    def test_filters_by_suffix(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        assert gr.list_files(suffixes=(".txt",)) == []


# ---------------------------------------------------------------------------
# Branch creation
# ---------------------------------------------------------------------------


class TestBranchCreation:
    def test_create_branch_from_main_returns_sha(
        self, repo_path: Path
    ) -> None:
        gr = GitRepo(repo_path=repo_path)
        sha = gr.create_branch_from_main("draft/test-1")
        assert gr.branch_exists("draft/test-1")
        assert gr.resolve_branch_sha("draft/test-1") == sha

    def test_duplicate_branch_raises(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        gr.create_branch_from_main("draft/dup")
        with pytest.raises(BranchAlreadyExistsError):
            gr.create_branch_from_main("draft/dup")


# ---------------------------------------------------------------------------
# Write + commit
# ---------------------------------------------------------------------------


class TestWriteAndCommit:
    def test_commit_persists_change(
        self,
        repo_path: Path,
        author: GitAuthor,
    ) -> None:
        gr = GitRepo(repo_path=repo_path)
        gr.create_branch_from_main("draft/edit-1")
        gr.write_file(
            "prompts/seed.md",
            "updated body\n",
            branch="draft/edit-1",
        )
        commit = gr.commit(
            "draft/edit-1",
            message="draft prompt change: prompts/seed.md",
            author=author,
        )
        assert commit.branch == "draft/edit-1"
        assert len(commit.short_sha) == 7
        # The committed content is reachable on the draft branch but
        # NOT on main — i.e. main is untouched (Requirement 2.2).
        assert gr.read_file(
            "prompts/seed.md",
            branch="draft/edit-1",
        ) == "updated body\n"
        assert gr.read_file(
            "prompts/seed.md",
            branch="main",
        ) == "seed body\n"

    def test_commit_creates_new_path(
        self,
        repo_path: Path,
        author: GitAuthor,
    ) -> None:
        gr = GitRepo(repo_path=repo_path)
        gr.create_branch_from_main("draft/new-file")
        gr.write_file(
            "prompts/new.md",
            "fresh content\n",
            branch="draft/new-file",
        )
        gr.commit(
            "draft/new-file",
            message="add prompts/new.md",
            author=author,
        )
        assert gr.read_file(
            "prompts/new.md",
            branch="draft/new-file",
        ) == "fresh content\n"

    def test_commit_without_writes_raises(
        self,
        repo_path: Path,
        author: GitAuthor,
    ) -> None:
        gr = GitRepo(repo_path=repo_path)
        gr.create_branch_from_main("draft/empty")
        with pytest.raises(GitRepoError):
            gr.commit(
                "draft/empty",
                message="no-op",
                author=author,
            )

    def test_path_traversal_rejected(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        gr.create_branch_from_main("draft/safe")
        with pytest.raises(GitRepoError):
            gr.write_file(
                "../etc/passwd",
                "evil",
                branch="draft/safe",
            )

    def test_write_on_missing_branch_raises(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        with pytest.raises(BranchNotFoundError):
            gr.write_file("x.md", "y", branch="ghost")


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_diff_empty_when_no_changes(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        gr.create_branch_from_main("draft/clean")
        # No commits on draft → diff is empty.
        assert gr.diff("draft/clean") == ""

    def test_diff_captures_change(
        self,
        repo_path: Path,
        author: GitAuthor,
    ) -> None:
        gr = GitRepo(repo_path=repo_path)
        gr.create_branch_from_main("draft/diff-1")
        gr.write_file(
            "prompts/seed.md",
            "different\n",
            branch="draft/diff-1",
        )
        gr.commit(
            "draft/diff-1",
            message="diff test",
            author=author,
        )
        diff = gr.diff("draft/diff-1")
        assert "different" in diff
        assert "seed body" in diff

    def test_diff_unknown_branch_raises(self, repo_path: Path) -> None:
        gr = GitRepo(repo_path=repo_path)
        with pytest.raises(BranchNotFoundError):
            gr.diff("ghost")
