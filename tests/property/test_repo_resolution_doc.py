"""Parity test for ``repo-resolution-order.md`` and prompts.

The repo resolution precedence is documented in three places that must
stay in sync:

1. ``platform/docs/api-contracts/repo-resolution-order.md`` — canonical
   contract (this is the source of truth).
2. ``platform/prompts/task_creation_assistant.md`` — user-facing prompt
   that tells task creators where to put the repo name.
3. ``platform/workers/agent-runner-worker/prompts/task_analysis.md`` —
   LLM decision prompt that picks the repo at runtime.

If any of the three drifts (different precedence numbers, different
source names, different fallback rules) the platform produces tasks
where the user, the assistant, and the bot disagree on what "repo"
means. This test catches drift early.

The test does not parse the markdown — that would require shipping a
markdown AST in CI. Instead we look for a small set of canonical
strings that MUST appear in each file. Drift = test fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_PLATFORM_ROOT = _THIS.parents[2]

_DOC_PATH = (
    _PLATFORM_ROOT / "docs" / "api-contracts" / "repo-resolution-order.md"
)
_TASK_CREATION_PROMPT = (
    _PLATFORM_ROOT / "prompts" / "task_creation_assistant.md"
)
_TASK_ANALYSIS_PROMPT = (
    _PLATFORM_ROOT
    / "workers"
    / "agent-runner-worker"
    / "prompts"
    / "task_analysis.md"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def doc_text() -> str:
    if not _DOC_PATH.is_file():
        pytest.skip(f"canonical doc not found at {_DOC_PATH}")
    return _DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def task_creation_text() -> str:
    if not _TASK_CREATION_PROMPT.is_file():
        pytest.skip(f"task creation prompt not found at {_TASK_CREATION_PROMPT}")
    return _TASK_CREATION_PROMPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def task_analysis_text() -> str:
    if not _TASK_ANALYSIS_PROMPT.is_file():
        pytest.skip(f"task analysis prompt not found at {_TASK_ANALYSIS_PROMPT}")
    return _TASK_ANALYSIS_PROMPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Doc internal consistency
# ---------------------------------------------------------------------------


def test_doc_lists_seven_sources(doc_text: str) -> None:
    """The canonical doc declares exactly 7 precedence levels.

    If you add or remove a source, also update the parametrised tests
    below — the count is part of the contract.
    """

    # The TL;DR table has rows numbered 1-7. We look for the literal
    # row markers ``| 1 |`` through ``| 7 |`` in the same document.
    for n in range(1, 8):
        marker = f"| {n} |"
        assert marker in doc_text, (
            f"Doc must list source #{n} as '{marker}' in the TL;DR table. "
            f"If you removed a source, update both the table and this test."
        )


def test_doc_does_not_list_eighth_source(doc_text: str) -> None:
    """An 8th row would be silent drift — bump this test deliberately."""

    assert "| 8 |" not in doc_text, (
        "Found '| 8 |' in the doc but the canonical contract is 7 sources. "
        "If you added a new source, update test_doc_lists_seven_sources, "
        "the parametrised parity tests below, AND both prompt files."
    )


# ---------------------------------------------------------------------------
# Doc ↔ prompt parity
# ---------------------------------------------------------------------------


# Canonical source labels that MUST appear in every file. These are the
# minimum tokens we cross-reference; the prose around them can vary.
_CANONICAL_SOURCE_TOKENS: tuple[str, ...] = (
    "custom field",       # #1
    "label",              # #2 + #4 mention
    "YAML front-matter",  # #3
    "single-repo",        # #5 (single-repo dept fallback)
    "needs_info",         # #7 (fallback to comment)
)


@pytest.mark.parametrize("token", _CANONICAL_SOURCE_TOKENS)
def test_doc_mentions_each_canonical_source_token(
    doc_text: str, token: str
) -> None:
    """Sanity: each token appears in the canonical doc body."""

    assert token.lower() in doc_text.lower(), (
        f"Canonical token {token!r} missing from "
        f"{_DOC_PATH.relative_to(_PLATFORM_ROOT)}. The token is part of the "
        f"contract this test enforces — either add it back or update "
        f"_CANONICAL_SOURCE_TOKENS."
    )


@pytest.mark.parametrize("token", _CANONICAL_SOURCE_TOKENS)
def test_task_creation_prompt_mentions_each_canonical_source_token(
    task_creation_text: str, token: str
) -> None:
    """The user-facing assistant prompt names each canonical source.

    Some tokens are shared with the YAML front-matter explanation
    (``YAML front-matter``); ``label``, ``custom field``, ``single-repo``
    and ``needs_info`` show up in the dedicated "Repo / Workspace / Branch"
    section.
    """

    assert token.lower() in task_creation_text.lower(), (
        f"Canonical token {token!r} missing from "
        f"{_TASK_CREATION_PROMPT.relative_to(_PLATFORM_ROOT)}. Drift between "
        f"the doc and the user-facing prompt would mean the assistant tells "
        f"users a different precedence rule than the bot actually applies."
    )


def test_task_analysis_prompt_mentions_target_repo_selection(
    task_analysis_text: str,
) -> None:
    """The LLM decision prompt explicitly walks the precedence list.

    We don't enforce every token here because the LLM prompt is
    deliberately compact; instead we check the section header that
    points at sources #5 and #6 of the doc.
    """

    needles = [
        "Target Repository Selection",
        "available_repos",
        "null",  # for non-code workflows
    ]
    for needle in needles:
        assert needle in task_analysis_text, (
            f"LLM decision prompt missing required token {needle!r} from "
            f"{_TASK_ANALYSIS_PROMPT.relative_to(_PLATFORM_ROOT)}. The prompt "
            f"and the canonical doc must describe the same selection rules."
        )
