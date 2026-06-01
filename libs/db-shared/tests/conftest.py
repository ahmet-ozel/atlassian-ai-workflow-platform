"""Pytest bootstrap for the db-shared library tests.

Adds ``libs/db-shared/src`` to ``sys.path`` so the tests can
``import db_shared`` without requiring an editable ``pip install``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PKG_SRC: Path = Path(__file__).resolve().parents[1] / "src"
if str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))
