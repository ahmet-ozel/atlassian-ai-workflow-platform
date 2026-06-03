"""CI gate — Task Creation Prompt kanonik bölüm başlıkları doğrulaması.


`prompts/task_creation_assistant.md` dosyasının 'te tanımlanan kanonik
bölüm başlıklarını doğru sırayla içerdiğini regex ile doğrular. Bölüm
sırası:

ROL → ZORUNLU OUTPUT FORMATI → WORKFLOW TYPE SEÇİM REHBERİ →
ZORUNLU SORU LİSTESİ → "Sizin Adınıza Yazabilir Miyim" → STANDALONE MOD →
ÖRNEK KONUŞMALAR → DEPARTMAN BİLGİLERİ → KURALLAR → SIK YAPILAN HATALAR →
DEĞİŞKEN ENJEKSİYONU
"""

from __future__ import annotations

import re
from pathlib import Path

# platform/ kök dizini (tests/ci/ → tests/ → platform/)
_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent

_CANONICAL_PATH = _PLATFORM_ROOT / "prompts" / "task_creation_assistant.md"

# kanonik bölüm başlıkları — sıralı
# Her biri markdown heading olarak (## veya daha derin) bulunmalı.
# Regex pattern'leri heading metninin ilgili anahtar kelimeyi İÇERMESİNİ arar;
# heading'de ek açıklama metni olabilir (ör. "STANDALONE MOD (...) — Z2").
_CANONICAL_SECTIONS = [
    (r"##\s+ROL\b", "ROL"),
    (r"##\s+ZORUNLU\s+OUTPUT\s+FORMATI", "ZORUNLU OUTPUT FORMATI"),
    (r"##\s+WORKFLOW\s+TYPE\s+SE.İM\s+REHBER", "WORKFLOW TYPE SEÇİM REHBERİ"),
    (r"##\s+ZORUNLU\s+SORU\s+LİSTESİ", "ZORUNLU SORU LİSTESİ"),
    (r'##\s+"?SİZİN\s+ADINIZA\s+YAZABİLİR\s+MİYİM"?', '"Sizin Adınıza Yazabilir Miyim"'),
    (r"##\s+STANDALONE\s+MOD", "STANDALONE MOD"),
    (r"##\s+ÖRNEK\s+KONUŞMALAR", "ÖRNEK KONUŞMALAR"),
    (r"##\s+DEPARTMAN\s+BİLGİLERİ", "DEPARTMAN BİLGİLERİ"),
    (r"##\s+KURALLAR", "KURALLAR"),
    (r"##\s+SIK\s+YAPILAN\s+HATALAR", "SIK YAPILAN HATALAR"),
    (r"##\s+DEĞİŞKEN\s+ENJEKSİYONU", "DEĞİŞKEN ENJEKSİYONU"),
]


def _find_section_position(content: str, pattern: str) -> int | None:
    """Bölüm başlığının dosyadaki karakter pozisyonunu döndürür."""
    heading_re = re.compile(pattern, re.MULTILINE)
    match = heading_re.search(content)
    return match.start() if match else None


def test_canonical_prompt_exists() -> None:
    """Kanonik prompt dosyası mevcut olmalı."""
    assert _CANONICAL_PATH.is_file(), (
        f"Kanonik prompt dosyası bulunamadı: {_CANONICAL_PATH}. "
        " gereği bu dosya tek kaynak olarak mevcut olmalıdır."
    )


def test_all_canonical_sections_present() -> None:
    """Tüm kanonik bölüm başlıkları dosyada mevcut olmalı."""
    content = _CANONICAL_PATH.read_text(encoding="utf-8")

    missing_sections: list[str] = []
    for pattern, label in _CANONICAL_SECTIONS:
        pos = _find_section_position(content, pattern)
        if pos is None:
            missing_sections.append(label)

    assert not missing_sections, (
        f"Kanonik prompt dosyasında şu bölüm başlıkları eksik: "
        f"{missing_sections}. Tüm bölümler mevcut olmalıdır."
    )


def test_canonical_sections_in_correct_order() -> None:
    """Kanonik bölüm başlıkları 'teki sırayla yer almalı."""
    content = _CANONICAL_PATH.read_text(encoding="utf-8")

    positions: list[tuple[str, int]] = []
    for pattern, label in _CANONICAL_SECTIONS:
        pos = _find_section_position(content, pattern)
        if pos is not None:
            positions.append((label, pos))

    # Sıralama kontrolü: her bölüm bir öncekinden sonra gelmeli
    for i in range(1, len(positions)):
        prev_name, prev_pos = positions[i - 1]
        curr_name, curr_pos = positions[i]
        assert curr_pos > prev_pos, (
            f"Bölüm sırası hatalı: '{curr_name}' (pos {curr_pos}) "
            f"'{prev_name}' (pos {prev_pos}) öncesinde yer alıyor. "
            f"Kanonik bölüm sırası ihlal edilmiş."
        )
