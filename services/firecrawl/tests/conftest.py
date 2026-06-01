"""Pytest discovery setup for the ``firecrawl`` service.

The wrapper ships its source under ``src/firecrawl`` and ``src/main.py``;
the workspace test runner does not install the package automatically, so we
prepend the ``src/`` directory to ``sys.path`` here. This mirrors the
pattern used by ``services/automation-service/tests/conftest.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_SRC = _SERVICE_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
