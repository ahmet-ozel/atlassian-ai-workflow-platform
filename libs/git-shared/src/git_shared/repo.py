"""Thin GitPython wrapper used by ``PromptsGitRouter``.

The wrapper exposes only the operations the prompt CRUD / PR flow
needs - it deliberately does not try to be a generic VCS client. The
small surface keeps tests focused and prevents accidental writes to
``main`` (every mutation goes through a draft branch).

The class is constructed once per process by the admin-dashboard-api
lifespan hook and re-used across requests. Every operation is
synchronous; the FastAPI router invokes the wrapper through
``await asyncio.to_thread(...)`` so a slow git call cannot stall the
event loop.

Operational references
----------------------
* Git CRUD endpoints (`/admin/prompts`, `/admin/prompts/{path:path}`,
  ``…/draft``, ``…/pr``).
* Template-format validation runs *before* any ``write_file`` call
  (the router enforces this; the wrapper keeps no validation
  responsibilities).

Worktree discipline
-------------------
Every mutation (`create_branch_from_main`, `write_file`, `commit`)
operates on a *named branch* without touching whatever the underlying
``git.Repo`` happens to have checked out. The wrapper:

1. Resolves the target branch to a commit SHA (creating the branch
   from ``main`` if requested).
2. Reads / writes file content via the in-memory index of that
   branch - no working-tree mutation.
3. Commits the new tree directly onto the branch ref using
   :class:`git.Tree` / :class:`git.IndexFile.write_tree` so the
   working directory the dev has open in their IDE never flickers.

This keeps the wrapper safe to call from a long-running FastAPI
process where the caller (the admin) and the host (the developer
running the service) may be different humans on different branches.
"""

from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

try:
    # GitPython is a hard runtime dependency. The import is wrapped in
    # a try/except so a misconfigured environment surfaces a clear
    # ``GitRepoError`` at construction time rather than a generic
    # ``ModuleNotFoundError`` mid-request.
    import git
    from git import GitCommandError, IndexFile, Repo
    from git.exc import InvalidGitRepositoryError, NoSuchPathError
    from gitdb.exc import BadName, BadObject
except ImportError as exc:  # pragma: no cover - exercised only on broken env
    raise RuntimeError(
        "git_shared requires GitPython>=3.1; install via "
        "`pip install GitPython`"
    ) from exc


# Tuple of exceptions GitPython can raise when a ref / commit cannot
# be resolved. ``rev_parse`` raises ``BadName`` / ``BadObject`` from
# ``gitdb`` - those are NOT subclasses of GitPython's own exception
# hierarchy, so we have to catch the union explicitly.
_REF_RESOLUTION_ERRORS = (GitCommandError, ValueError, BadName, BadObject)

from .errors import (
    BranchAlreadyExistsError,
    BranchNotFoundError,
    GitRepoError,
    MergeConflictError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitAuthor:
    """Commit author / committer identity.

    Maps onto :class:`git.Actor`. The router builds one of these from
    the OIDC ``AuthContext`` (``actor_id`` → ``email`` derived from
    the ``sub`` claim, full name from the token's ``name`` claim).
    """

    name: str
    email: str


@dataclass(frozen=True)
class GitCommit:
    """Result of :meth:`GitRepo.commit`.

    Carries enough metadata for the router to emit a
    ``prompt_draft_created`` audit row and to reference the commit
    hash in subsequent calls (eg. ``read_file(branch=...)``).
    """

    branch: str
    sha: str
    short_sha: str
    message: str


# ---------------------------------------------------------------------------
# GitRepo
# ---------------------------------------------------------------------------


# Default branch we treat as the merge target. Production deployments
# that use a different default branch can override via the
# ``main_branch`` ctor parameter without changing the public API.
_DEFAULT_MAIN_BRANCH = "main"


class GitRepo:
    """Thin GitPython adapter for the prompt CRUD / PR flow.

    The class is intentionally narrow: every method maps onto exactly
    one of the steps the :class:`PromptsGitRouter` performs:

    * :meth:`read_file`                 - `GET /admin/prompts/{path}`
    * :meth:`list_files`                - `GET /admin/prompts` (filter
      by extension).
    * :meth:`create_branch_from_main`   - `POST .../draft` step 1.
    * :meth:`write_file`                - `POST .../draft` step 2.
    * :meth:`commit`                    - `POST .../draft` step 3.
    * :meth:`diff`                      - `POST .../pr` step 1
      (description renderer input).
    * :meth:`branch_exists` /
      :meth:`resolve_branch_sha`        - guard helpers used by the
      router for idempotent reads.

    The wrapper does NOT touch the working directory of the underlying
    repo. Every mutation goes through ``IndexFile`` so the host's
    checked-out branch and any unstaged changes remain untouched.

    Args:
        repo_path: Absolute path to the local clone. The directory
            must already be a git repository; a ``GitRepoError`` is
            raised otherwise.
        main_branch: Name of the branch ``create_branch_from_main``
            forks from. Defaults to ``"main"``; production deployments
            using ``"master"`` or another default override here.
    """

    def __init__(
        self,
        *,
        repo_path: Path | str,
        main_branch: str = _DEFAULT_MAIN_BRANCH,
    ) -> None:
        self._repo_path = Path(repo_path).resolve()
        self._main_branch = main_branch
        try:
            self._repo: Repo = Repo(str(self._repo_path))
        except (InvalidGitRepositoryError, NoSuchPathError) as exc:
            raise GitRepoError(
                f"{self._repo_path!s} is not a git repository: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    @property
    def repo_path(self) -> Path:
        """Absolute path to the underlying clone."""

        return self._repo_path

    @property
    def main_branch(self) -> str:
        """Configured main branch (default ``"main"``)."""

        return self._main_branch

    def branch_exists(self, name: str) -> bool:
        """Return ``True`` if ``name`` is a local branch ref."""

        return name in (head.name for head in self._repo.heads)

    def resolve_branch_sha(self, branch: str) -> str:
        """Return the commit SHA at the tip of ``branch``.

        Raises :class:`BranchNotFoundError` when the branch does not
        exist locally.
        """

        if not self.branch_exists(branch):
            raise BranchNotFoundError(f"branch not found: {branch!r}")
        return self._repo.heads[branch].commit.hexsha

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    def list_files(
        self,
        *,
        branch: str | None = None,
        path_prefix: str = "",
        suffixes: Iterable[str] = (".md",),
    ) -> list[str]:
        """List repository files at ``branch`` matching the filter.

        Args:
            branch: Branch to list from. Defaults to ``main_branch``.
            path_prefix: Only return paths that start with this
                prefix (eg. ``"prompts/"``). Empty string returns
                every file.
            suffixes: File extensions accepted (case-sensitive). The
                default ``(".md",)`` matches every prompt file.

        Returns:
            Sorted list of POSIX-style relative paths.
        """

        target = branch or self._main_branch
        try:
            commit = self._repo.commit(target)
        except _REF_RESOLUTION_ERRORS as exc:
            raise BranchNotFoundError(
                f"cannot resolve branch {target!r}: {exc}"
            ) from exc

        suffix_tuple = tuple(suffixes)
        results: list[str] = []
        for blob in commit.tree.traverse():
            if getattr(blob, "type", None) != "blob":
                continue
            posix = blob.path  # GitPython exposes paths as POSIX strings
            if path_prefix and not posix.startswith(path_prefix):
                continue
            if suffix_tuple and not posix.endswith(suffix_tuple):
                continue
            results.append(posix)
        results.sort()
        return results

    def read_file(
        self,
        path: str,
        *,
        branch: str | None = None,
    ) -> str:
        """Return the UTF-8 content of ``path`` at ``branch``.

        Args:
            path: Repository-relative POSIX-style path.
            branch: Branch to read from. Defaults to ``main_branch``.

        Raises:
            BranchNotFoundError: When ``branch`` does not resolve.
            FileNotFoundError: When ``path`` is not present in the
                branch's tree.
        """

        target = branch or self._main_branch
        try:
            commit = self._repo.commit(target)
        except _REF_RESOLUTION_ERRORS as exc:
            raise BranchNotFoundError(
                f"cannot resolve branch {target!r}: {exc}"
            ) from exc

        try:
            blob = commit.tree / path
        except KeyError as exc:
            raise FileNotFoundError(
                f"path {path!r} not found in branch {target!r}"
            ) from exc

        # GitPython's blob.data_stream returns bytes-compatible chunks;
        # decoding here keeps the public API string-only.
        raw = blob.data_stream.read()
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)

    # ------------------------------------------------------------------
    # Mutation surface (all operate via IndexFile, working tree-safe)
    # ------------------------------------------------------------------

    def create_branch_from_main(self, name: str) -> str:
        """Create ``name`` as a fresh branch off ``main_branch``.

        Returns the SHA the branch was created at.

        Raises:
            BranchAlreadyExistsError: When a local branch with that
                name already exists. The router should pick a fresh
                ``draft/<actor>-<ts>`` name and retry.
            BranchNotFoundError: When ``main_branch`` is missing - a
                misconfigured clone.
        """

        if self.branch_exists(name):
            raise BranchAlreadyExistsError(
                f"branch {name!r} already exists"
            )
        try:
            main_commit = self._repo.commit(self._main_branch)
        except _REF_RESOLUTION_ERRORS as exc:
            raise BranchNotFoundError(
                f"main branch {self._main_branch!r} not found: {exc}"
            ) from exc

        # ``create_head`` creates the ref pointing at ``main_commit``
        # without checking anything out into the working tree.
        new_head = self._repo.create_head(name, commit=main_commit)
        return new_head.commit.hexsha

    def write_file(
        self,
        path: str,
        body: str,
        *,
        branch: str,
    ) -> None:
        """Stage ``body`` as the new content of ``path`` on ``branch``.

        The change is buffered on the in-memory :class:`IndexFile`
        bound to ``branch``; call :meth:`commit` to persist. Multiple
        ``write_file`` calls on the same branch in a single request
        are batched into one commit.

        Args:
            path: Repository-relative POSIX path. Parent directories
                are created implicitly inside the tree (git does not
                track directories so no on-disk action is taken).
            body: UTF-8 content to write.
            branch: Branch to stage the change against. Must exist.

        Raises:
            BranchNotFoundError: When ``branch`` does not exist.
        """

        if not self.branch_exists(branch):
            raise BranchNotFoundError(
                f"cannot write_file on missing branch {branch!r}"
            )

        # Lazily create / reuse a per-branch staging buffer. The map
        # lives on the instance so a single request that calls
        # write_file → write_file → commit operates on the same index.
        pending = self._pending().setdefault(branch, {})
        pending[self._normalise_path(path)] = body.encode("utf-8")

    def commit(
        self,
        branch: str,
        *,
        message: str,
        author: GitAuthor,
    ) -> GitCommit:
        """Persist every pending :meth:`write_file` onto ``branch``.

        Builds a fresh tree from the branch's tip + the staged
        overrides and points ``branch`` at the new commit. The
        underlying repository's working tree is untouched.

        Args:
            branch: Target branch (must already exist).
            message: Commit message. The router supplies a stable
                shape (``"draft prompt change: <path>"``).
            author: :class:`GitAuthor` whose ``name`` / ``email`` are
                used for both author *and* committer.

        Returns:
            :class:`GitCommit` with branch / SHA / message metadata.

        Raises:
            BranchNotFoundError: When ``branch`` is missing.
            GitRepoError: When no ``write_file`` calls were buffered
                or when GitPython rejects the commit (eg. invalid
                tree).
        """

        if not self.branch_exists(branch):
            raise BranchNotFoundError(
                f"cannot commit on missing branch {branch!r}"
            )

        pending = self._pending().get(branch)
        if not pending:
            raise GitRepoError(
                f"no staged changes for branch {branch!r}; "
                "call write_file first"
            )

        actor = git.Actor(author.name, author.email)
        head = self._repo.heads[branch]
        parent_commit = head.commit

        # Build a fresh index from the parent tree, override the staged
        # paths, write the tree and commit it. ``IndexFile.from_tree``
        # produces an in-memory index seeded with the parent's tree -
        # the on-disk index of the repo is left alone. We then mutate
        # ``index.entries`` in place (the IndexFile API exposes it as
        # a public dict) and call ``write_tree()`` to materialise.
        try:
            index = IndexFile.from_tree(self._repo, parent_commit)
            for path, content in pending.items():
                blob_sha = self._write_blob(content)
                self._index_set_blob(index, path, blob_sha)
            tree_sha = index.write_tree().hexsha
        except GitCommandError as exc:  # pragma: no cover - GitPython error path
            raise GitRepoError(f"failed to build commit tree: {exc}") from exc

        # ``Commit.create_from_tree`` writes the commit object. We
        # pass ``head=False`` to avoid mutating the underlying
        # ``HEAD`` ref (the host's working tree may be checked out
        # on a different branch); we advance ``branch`` ourselves
        # via ``head.set_commit`` below.
        commit = git.Commit.create_from_tree(
            self._repo,
            tree=tree_sha,
            message=message,
            parent_commits=[parent_commit],
            head=False,
            author=actor,
            committer=actor,
        )

        # Advance the branch ref to the new commit.
        head.set_commit(commit)

        # Drain the per-branch buffer.
        self._pending().pop(branch, None)

        sha = commit.hexsha
        return GitCommit(
            branch=branch,
            sha=sha,
            short_sha=sha[:7],
            message=message,
        )

    # ------------------------------------------------------------------
    # Diff helpers
    # ------------------------------------------------------------------

    def diff(self, branch: str, *, against: str | None = None) -> str:
        """Return a unified-format diff of ``branch`` vs ``against``.

        Args:
            branch: Source branch (the draft).
            against: Target branch. Defaults to ``main_branch``.

        Returns:
            Unified-diff text. Empty string when the branches are
            identical.

        Raises:
            BranchNotFoundError: When either branch is missing.
        """

        target = against or self._main_branch
        for name in (branch, target):
            if not self.branch_exists(name):
                raise BranchNotFoundError(f"branch not found: {name!r}")

        # ``git diff target..source`` is the "what does source add
        # over target" shape we want for PR descriptions.
        try:
            return self._repo.git.diff(f"{target}..{branch}")
        except GitCommandError as exc:  # pragma: no cover
            raise GitRepoError(f"failed to compute diff: {exc}") from exc

    def detect_merge_conflict(
        self,
        branch: str,
        *,
        against: str | None = None,
    ) -> bool:
        """Return ``True`` when merging ``branch`` into ``against`` would conflict.

        Performs a ``git merge-tree`` (no-touch merge dry-run). The
        method is read-only - neither branch is modified.

        Raises :class:`BranchNotFoundError` when either branch is
        missing. On unexpected GitPython failures the underlying
        :class:`GitCommandError` is re-raised as :class:`GitRepoError`
        so callers do not need to import ``git`` directly.
        """

        target = against or self._main_branch
        for name in (branch, target):
            if not self.branch_exists(name):
                raise BranchNotFoundError(f"branch not found: {name!r}")

        try:
            base = self._repo.git.merge_base(target, branch).strip()
        except GitCommandError:
            # No common ancestor → treat as conflict (defensive).
            return True

        try:
            output = self._repo.git.merge_tree(base, target, branch)
        except GitCommandError as exc:  # pragma: no cover - rare git error
            raise GitRepoError(f"merge_tree failed: {exc}") from exc

        # ``git merge-tree`` emits ``<<<<<<<`` / ``>>>>>>>`` markers in
        # its output when conflicts are detected.
        return "<<<<<<< " in output or "=======" in output and "+<<<<" in output

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pending(self) -> dict[str, dict[str, bytes]]:
        """Return the per-branch staging buffer (lazily created)."""

        if not hasattr(self, "_pending_writes"):
            self._pending_writes: dict[str, dict[str, bytes]] = {}
        return self._pending_writes

    @staticmethod
    def _normalise_path(path: str) -> str:
        """Return ``path`` as a POSIX-style relative path with no leading slash."""

        cleaned = path.replace("\\", "/").lstrip("/")
        # Reject path traversal - the router already validates this,
        # but a defence-in-depth check here keeps the lib safe to use
        # from non-router callers.
        if ".." in cleaned.split("/"):
            raise GitRepoError(
                f"path {path!r} must not contain '..' segments"
            )
        return cleaned

    def _write_blob(self, content: bytes) -> str:
        """Write ``content`` as a fresh blob object and return its SHA.

        Uses ``Repo.odb.store`` (the gitdb-backed object database)
        rather than ``git hash-object`` so the call is in-process and
        does not rely on subprocess stdin (which doesn't accept
        BytesIO on Windows). The result is byte-for-byte identical
        to what ``hash-object -w --stdin`` would produce - git's
        blob hash is deterministic.
        """

        from gitdb import IStream

        stream = IStream("blob", len(content), io.BytesIO(content))
        self._repo.odb.store(stream)
        # ``IStream.binsha`` is populated in-place after store().
        return stream.binsha.hex()

    @staticmethod
    def _index_set_blob(index: "IndexFile", path: str, blob_sha: str) -> None:
        """Replace / insert ``path`` in ``index`` with ``blob_sha``.

        Implemented as a static helper so the commit path stays
        readable. ``IndexFile.entries`` is a dict keyed on
        ``(path, stage)``; we mutate it in-place. The entry tuple
        shape is ``(mode, binsha, flags, path)`` where ``flags``
        encodes the stage in the high bits via ``CE_STAGESHIFT``.
        """

        from git.index.typ import BaseIndexEntry, CE_STAGESHIFT

        # Remove any existing entry at this path (any stage) - git
        # indexes can carry stage 1/2/3 entries during merges, but for
        # our flat write-then-commit flow stage 0 is the only valid
        # outcome.
        for key in list(index.entries.keys()):
            if key[0] == path:
                del index.entries[key]

        stage = 0
        # Mode 0o100644 (regular file, non-executable) is correct for
        # every prompt Markdown file. The 4-tuple constructor is the
        # public entry-point documented on ``BaseIndexEntry``.
        entry = BaseIndexEntry(
            (
                0o100644,                         # mode
                bytes.fromhex(blob_sha),          # binsha (20 bytes)
                stage << CE_STAGESHIFT,           # flags (stage in high bits)
                path,                             # path (POSIX)
            )
        )
        index.entries[(path, stage)] = entry


__all__ = ["GitAuthor", "GitCommit", "GitRepo"]
