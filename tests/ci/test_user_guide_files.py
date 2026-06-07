"""CI gate - end-user docs catalog (ops work / W2).


Every page documented in design.md §"User Guide" MUST exist under
``platform/docs/user-guide/`` and carry non-empty content. The list is
pinned here so a future deletion / rename surfaces as a CI failure
instead of leaving end users without operating instructions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_USER_GUIDE_DIR = _PLATFORM_ROOT / "docs" / "user-guide"

#: The five canonical user-guide pages (W2 backlog item).
_REQUIRED_PAGES: tuple[str, ...] = (
    "README.md",
    "task-creation.md",
    "waiting-for-bot.md",
    "iteration-with-comments.md",
    "what-bot-cannot-do.md",
    "faq.md",
)


def test_user_guide_directory_exists() -> None:
    assert _USER_GUIDE_DIR.is_dir(), (
        f"Missing platform/docs/user-guide/ - the user guide catalog needs the "
        f"end-user documentation tree at {_USER_GUIDE_DIR}."
    )


@pytest.mark.parametrize("page", _REQUIRED_PAGES)
def test_required_user_guide_page_exists_and_is_non_empty(page: str) -> None:
    path = _USER_GUIDE_DIR / page
    assert path.is_file(), (
        f"Missing user-guide page {page!r}; the user guide catalog requires "
        "this file to be present so end users have stable links."
    )
    body = path.read_text(encoding="utf-8").strip()
    assert len(body) > 100, (
        f"User-guide page {page!r} is suspiciously short "
        f"({len(body)} bytes); fill it in or delete the entry from "
        "the canonical list."
    )
