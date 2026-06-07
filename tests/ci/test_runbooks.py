"""CI gate - operator runbooks (ops work).


Operator-facing runbooks under ``platform/docs/runbooks/`` MUST be
present, parse as Markdown (we just probe non-empty bodies here)
and carry a "## Symptoms" or "## Steps" header so they read like an
actual runbook instead of a placeholder TODO.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_RUNBOOKS_DIR = _PLATFORM_ROOT / "docs" / "runbooks"

#: Canonical runbook list. Every entry refers to a runbook the
#: design document mandates; the file MAY ship an `# Title` heading
#: but the structural sniff below is enough for the CI gate.
_REQUIRED_RUNBOOKS: tuple[str, ...] = (
    "dept-decommission.md",
)


def test_runbooks_directory_exists() -> None:
    assert _RUNBOOKS_DIR.is_dir(), (
        f"Missing platform/docs/runbooks/ - the runbook catalog needs the "
        f"operator runbook tree at {_RUNBOOKS_DIR}."
    )


@pytest.mark.parametrize("runbook", _REQUIRED_RUNBOOKS)
def test_required_runbook_exists_and_has_actionable_content(
    runbook: str,
) -> None:
    path = _RUNBOOKS_DIR / runbook
    assert path.is_file(), f"Missing runbook {runbook!r} ."
    body = path.read_text(encoding="utf-8")
    assert len(body) > 500, (
        f"Runbook {runbook!r} is too short to be useful "
        f"({len(body)} bytes)."
    )
    # Sniff for at least one of the canonical structural headers.
    # Accept either English (Symptoms / Steps / …) or Turkish
    # equivalents (Adım / Akış / Sorun giderme / Ne zaman kullanılır).
    has_section = any(
        marker in body
        for marker in (
            "## Symptoms",
            "## Steps",
            "## Triggered by",
            "## Action",
            "## Adım",
            "## Akış",
            "## Genel Akış",
            "## Sorun giderme",
            "## Ne zaman kullanılır",
            "## Ön gereksinimler",
        )
    )
    assert has_section, (
        f"Runbook {runbook!r} is missing the canonical section "
        "headers (Symptoms / Steps / Triggered by / Action / Adım / "
        "Akış / Sorun giderme). Without structure the document is "
        "hard to follow under pressure."
    )
