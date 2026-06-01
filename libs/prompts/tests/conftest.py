"""Pytest bootstrap for the prompts library tests.

Adds ``libs/prompts/src`` to ``sys.path`` so the tests can
``import prompts`` without requiring an editable ``pip install``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PKG_SRC: Path = Path(__file__).resolve().parents[1] / "src"
if str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))
