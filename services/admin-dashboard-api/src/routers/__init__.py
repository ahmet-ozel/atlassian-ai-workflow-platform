"""Admin API routers.

Sub-modules host the FastAPI ``APIRouter`` instances backing the
Next.js admin dashboard pages (services, workflows, departments,
prompts, audit, costs, notifications, security, feature-flags,
firecrawl allowlist, setup wizard, test results).

Each router is imported lazily by ``src.main`` with a soft-fail
pattern so a missing or broken module does not block the rest of
the admin surface. The exports below are convenience re-exports
for callers that prefer ``from src.routers import firecrawl_allowlist``
over reaching into the module path directly. Imports here are
guarded so the package stays importable when an individual router
module fails to load (eg. while a feature is in flight).
"""

from __future__ import annotations

# platform-completion task 26.2 — register the three new routers
# (firecrawl allowlist, setup wizard, test results) alongside the
# existing ones so the admin dashboard surface picks them up.
try:  # pragma: no cover - exercised by unit tests
    from . import firecrawl_allowlist  # noqa: F401
except Exception:  # noqa: BLE001 - soft-fail when the module is absent
    firecrawl_allowlist = None  # type: ignore[assignment]

try:  # pragma: no cover - exercised by unit tests
    from . import setup_wizard  # noqa: F401
except Exception:  # noqa: BLE001
    setup_wizard = None  # type: ignore[assignment]

try:  # pragma: no cover - exercised by unit tests
    from . import test_results  # noqa: F401
except Exception:  # noqa: BLE001
    test_results = None  # type: ignore[assignment]

# platform-gap-fill task 9.1 — capability probe matrix router. Soft
# imported so the rest of the admin surface stays available even if
# the module fails to load (eg. while task 9.3's asyncpg adapter is
# in flight and one of its imports breaks).
try:  # pragma: no cover - exercised by unit tests
    from . import capabilities  # noqa: F401
except Exception:  # noqa: BLE001
    capabilities = None  # type: ignore[assignment]


__all__ = (
    "firecrawl_allowlist",
    "setup_wizard",
    "test_results",
    "capabilities",
)
