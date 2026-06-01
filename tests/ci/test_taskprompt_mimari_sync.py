"""V15 CI gate: prompt MD ↔ ``MIMARI.md`` backlog ID sync.

**Validates: Requirements 2.8** (``platform-mimari-ops``) /
MIMARI §16.14.15 V15.

Every prompt Markdown file shipped in the platform tree (under any
``prompts/`` directory — ``platform/prompts/``,
``platform/services/<svc>/prompts/``, ``platform/workers/<svc>/prompts/``)
that mentions a backlog ID matching the regex
``\\b([XYZNVWGSBEQRT]\\d{1,2})\\b`` MUST also have that ID present in
the workspace-root ``MIMARI.md``. The regex character class lists
every backlog series letter the architecture document tracks (``V``,
``Y``, ``B``, ``N``, ``T``, ``X``, ``Z``, ``W``, ``G``, ``S``, ``E``,
``Q``, ``R``); the ``\\d{1,2}`` suffix matches the 1-2 digit numeric
ID. The regex is **identical** to the one mirrored by the
admin-dashboard-api PR renderer (``services/admin-dashboard-api/src/
prompts/pr_renderer.py``: ``_V15_ID_RE``) so the static analyser, the
PR description, and this CI gate can never disagree on the ID set.

When a prompt body references a backlog ID that does not appear in
``MIMARI.md`` the build fails with a list of ``(prompt path, missing
IDs)`` pairs. The fix is one of:

1. Document the missing ID in ``MIMARI.md`` (the usual case — every
   backlog item is supposed to be cross-linked from the architecture
   document).
2. Remove the orphan ID from the prompt body (rare — only when the
   ID was a typo).

The gate intentionally walks the platform tree at runtime rather than
hard-coding the prompt list so newly added prompt files are picked up
without changes here. ``libs/prompts/README.md`` is excluded because
``libs/prompts/`` is the *library* named "prompts" (the ``PromptLoader``
package) — not a directory of prompt templates. Vendored
``site-packages`` and virtual-env trees under
``services/atlassian_unified/.venv`` are also excluded.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Regex — verbatim mirror of the documented Requirement 2.8 / V15 pattern.
# Mirrors ``_V15_ID_RE`` in ``services/admin-dashboard-api/src/prompts/
# pr_renderer.py``; both must stay in sync.
# ---------------------------------------------------------------------------

_V15_ID_RE: re.Pattern[str] = re.compile(r"\b([XYZNVWGSBEQRT]\d{1,2})\b")


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _platform_root() -> Path:
    """Return the workspace-root ``platform/`` directory.

    ``conftest.py`` sets ``WORKSPACE_ROOT`` to the directory holding
    ``pytest.ini`` (i.e. ``platform/``). We re-derive it locally so
    this gate stays runnable as a plain ``python -m pytest`` invocation
    even if the conftest is bypassed.
    """

    return Path(__file__).resolve().parent.parent.parent


def _repo_root() -> Path:
    """Return the workspace root that contains ``MIMARI.md``.

    The architecture document lives one directory above ``platform/``
    (``c:\\...\\yeni_atlassian\\MIMARI.md``).
    """

    return _platform_root().parent


def _is_excluded(path: Path, platform_root: Path) -> bool:
    """Return True for paths that look like prompts but aren't.

    Exclusions:

    * ``libs/prompts/`` — the *library* named "prompts" (PromptLoader
      package), not a directory of prompt templates.
    * Anything under a ``.venv`` or ``site-packages`` segment — vendored
      third-party Markdown.
    * ``__pycache__`` and other dotted directories — defensive.
    """

    rel_parts = path.relative_to(platform_root).parts
    if rel_parts and rel_parts[0] == "libs" and len(rel_parts) > 1 and rel_parts[1] == "prompts":
        return True
    forbidden = {".venv", "site-packages", "__pycache__", ".pytest_cache", ".hypothesis"}
    return any(part in forbidden for part in rel_parts)


def _discover_prompt_files(platform_root: Path) -> tuple[Path, ...]:
    """Walk the platform tree and collect every ``prompts/**/*.md`` file.

    The discovery rule mirrors the design's prompt storage convention:
    prompt templates live in directories *named* ``prompts/`` at three
    canonical locations — workspace root (``platform/prompts/``),
    per-service (``services/<svc>/prompts/``), and per-worker
    (``workers/<svc>/prompts/``). Any ``.md`` reachable from a
    ``prompts/`` segment qualifies. Library/test directories that
    happen to be *named* ``prompts`` (e.g. ``libs/prompts/``) are
    filtered out by :func:`_is_excluded`.

    Results are returned sorted (POSIX-relative) for deterministic
    failure messages.
    """

    matches: list[Path] = []
    for md_path in platform_root.rglob("*.md"):
        if _is_excluded(md_path, platform_root):
            continue
        # The file must sit under at least one ``prompts/`` segment.
        if "prompts" not in md_path.relative_to(platform_root).parts:
            continue
        matches.append(md_path)
    return tuple(sorted(matches))


def _extract_backlog_ids(text: str) -> tuple[str, ...]:
    """Return the sorted-unique set of V15 backlog IDs in ``text``."""

    return tuple(sorted(set(_V15_ID_RE.findall(text))))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mimari_text() -> str:
    """Read ``MIMARI.md`` once per module run.

    Missing file is a hard fail — the gate cannot make any claim
    without the architecture document. (This differs from the
    PR-renderer helper which fail-soft when MIMARI is unavailable;
    the CI gate has stronger guarantees because it owns the build.)
    """

    mimari_path = _repo_root() / "MIMARI.md"
    assert mimari_path.is_file(), (
        f"MIMARI.md not found at {mimari_path}. The V15 sync gate "
        "requires the workspace-root architecture document; cannot "
        "verify backlog ID coverage without it."
    )
    return mimari_path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def prompt_files() -> tuple[Path, ...]:
    """Discover every prompt Markdown file under ``platform/``."""

    return _discover_prompt_files(_platform_root())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_collection_finds_at_least_the_assistant_chat_prompt(
    prompt_files: tuple[Path, ...],
) -> None:
    """Discovery sanity check.

    The platform tree always ships at least ``platform/prompts/
    assistant_chat.md`` (created by task 4.5). If the discovery walk
    returns an empty tuple something has shifted in the layout — fail
    loudly so the V15 assertion below is not silently skipped.
    """

    platform_root = _platform_root()
    assert prompt_files, (
        "No prompt Markdown files discovered under "
        f"{platform_root}. Expected at least "
        "platform/prompts/assistant_chat.md."
    )
    rel_paths = {p.relative_to(platform_root).as_posix() for p in prompt_files}
    assert "prompts/assistant_chat.md" in rel_paths, (
        "platform/prompts/assistant_chat.md missing from the "
        f"V15 gate's discovery walk. Discovered: {sorted(rel_paths)}"
    )


def test_every_prompt_backlog_id_is_documented_in_mimari(
    prompt_files: tuple[Path, ...],
    mimari_text: str,
) -> None:
    """V15 CI gate.

    For every prompt Markdown file under ``platform/`` that is reachable
    via a ``prompts/`` segment, every backlog ID matching the regex
    ``\\b([XYZNVWGSBEQRT]\\d{1,2})\\b`` MUST also appear (verbatim) in
    ``MIMARI.md``. Failure surfaces a per-file list of orphan IDs so
    the developer knows exactly what to document or remove.

    Validates: Requirements 2.8.
    """

    platform_root = _platform_root()
    orphans: dict[str, tuple[str, ...]] = {}

    for prompt_path in prompt_files:
        body = prompt_path.read_text(encoding="utf-8")
        ids = _extract_backlog_ids(body)
        if not ids:
            continue
        # ``in`` substring search is sufficient: MIMARI uses the same
        # regex shape and embeds IDs as standalone tokens (e.g.
        # ``§16.14.2 V2``); a substring match cannot produce false
        # positives because ``\d{1,2}`` is bounded — ``V15`` matches
        # only ``V15`` (not ``V150``) thanks to the regex word-boundary
        # on the discovery side, and MIMARI documents IDs explicitly.
        missing = tuple(_id for _id in ids if _id not in mimari_text)
        if missing:
            rel = prompt_path.relative_to(platform_root).as_posix()
            orphans[rel] = missing

    if orphans:
        lines = ["The following backlog IDs are missing from MIMARI.md:"]
        for rel, missing in sorted(orphans.items()):
            lines.append(f"  {rel}: {', '.join(missing)}")
        lines.append(
            "Document each ID in MIMARI.md or remove it from the "
            "prompt body. (V15 CI gate / Requirement 2.8.)"
        )
        pytest.fail("\n".join(lines))


def test_v15_regex_matches_documented_pattern() -> None:
    """Regression guard: the regex literal must stay verbatim.

    ``Requirement 2.8`` pins the pattern as
    ``\\b([XYZNVWGSBEQRT]\\d{1,2})\\b``. If a future contributor edits
    the literal (e.g. adds a new series letter) without updating the
    requirements document, this test catches the drift.
    """

    assert _V15_ID_RE.pattern == r"\b([XYZNVWGSBEQRT]\d{1,2})\b"
