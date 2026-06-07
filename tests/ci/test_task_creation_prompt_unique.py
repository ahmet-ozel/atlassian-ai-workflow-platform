"""CI gate - Task Creation Prompt tek kanonik kaynak doğrulaması.


`docs/task-creation-assistant-prompt.md` dosyası ya mevcut olmamalı ya da
yalnızca `prompts/task_creation_assistant.md`'ye yönlendiren ≤5 satırlık
redirect stub olmalıdır. Her iki dosyanın da ana içerikle dolu olması
CI fail'dir - tek kanonik kaynak kuralı ihlal edilmiş demektir.
"""

from __future__ import annotations

import re
from pathlib import Path

# platform/ kök dizini (tests/ci/  tests/  platform/)
_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent

_OLD_DOCS_PATH = _PLATFORM_ROOT / "docs" / "task-creation-assistant-prompt.md"
_CANONICAL_PATH = _PLATFORM_ROOT / "prompts" / "task_creation_assistant.md"

# Redirect stub'ın `prompts/task_creation_assistant.md`'ye link içermesi beklenir
_REDIRECT_PATTERN = re.compile(
    r"prompts/task_creation_assistant\.md", re.IGNORECASE
)

# Stub dosyası en fazla 5 satır olmalı
_MAX_STUB_LINES = 5


def test_old_docs_file_is_absent_or_redirect_stub() -> None:
    """docs/task-creation-assistant-prompt.md ya yok ya da ≤5 satırlık redirect."""
    if not _OLD_DOCS_PATH.exists():
        # Dosya yok - tamamen kabul edilebilir
        return

    content = _OLD_DOCS_PATH.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]

    assert len(lines) <= _MAX_STUB_LINES, (
        f"docs/task-creation-assistant-prompt.md {len(lines)} boş olmayan satır "
        f"içeriyor (max {_MAX_STUB_LINES}). Bu dosya yalnızca redirect stub "
        "olmalı - ana içerik prompts/task_creation_assistant.md'de tutulmalı."
    )

    assert _REDIRECT_PATTERN.search(content), (
        "docs/task-creation-assistant-prompt.md mevcut ama "
        "'prompts/task_creation_assistant.md' yönlendirme linki içermiyor. "
        "Stub dosyası kanonik kaynağa link vermelidir."
    )


def test_canonical_prompt_exists() -> None:
    """Kanonik kaynak prompts/task_creation_assistant.md mevcut olmalı."""
    assert _CANONICAL_PATH.is_file(), (
        f"Kanonik prompt dosyası bulunamadı: {_CANONICAL_PATH}. "
        " gereği bu dosya tek kaynak olarak mevcut olmalıdır."
    )


def test_both_files_not_full_content() -> None:
    """Her iki dosya da ana içerikle dolu olmamalı (çelişki riski)."""
    if not _OLD_DOCS_PATH.exists():
        return

    old_content = _OLD_DOCS_PATH.read_text(encoding="utf-8")
    old_lines = [line for line in old_content.splitlines() if line.strip()]

    # Eğer eski dosya 5 satırdan fazlaysa, ana içerikle dolu demektir
    # Bu durumda kanonik dosya ile çelişki riski var
    if len(old_lines) > _MAX_STUB_LINES:
        assert not _CANONICAL_PATH.is_file() or _CANONICAL_PATH.stat().st_size < 100, (
            "HER İKİ dosya da ana içerikle dolu! "
            "docs/task-creation-assistant-prompt.md ≤5 satırlık redirect stub'a "
            "indirilmeli veya silinmeli . Tek kanonik kaynak kuralı ihlal."
        )
