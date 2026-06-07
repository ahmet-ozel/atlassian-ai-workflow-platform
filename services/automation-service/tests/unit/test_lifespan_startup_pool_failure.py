"""Startup propagates pool failure with cleanup.

When the lifespan handler's pool construction raises, the original exception
MUST propagate out of the context manager's ``__aenter__`` and every resource
successfully constructed before the failing step MUST be closed before the
exception escapes. No production-wired ``app.state.<slot>`` should be populated.

The handler builds resources in this order:

1. ``httpx.AsyncClient`` - succeeds, must be ``aclose()``-d on failure
2. ``asyncpg.create_pool`` - RAISES the test's ``OSError``
3. ``vault_factory.make_client`` - never reached
4. ``AuditLogger`` - never reached
5. ``TemporalClient`` + ``connect()`` - never reached
6. … etc.

So the assertion is: ``http_client.aclose()`` was awaited exactly once
(the only resource constructed before the pool), no other closer ran,
the lifespan re-raised the exact ``OSError``, and no slot was populated.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


# Bootstrap ``src`` onto ``sys.path``.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


import automation_service  # noqa: E402,F401
app_module = sys.modules["automation_service.app"]


class _CountingHttpClient:
    """``httpx.AsyncClient`` stand-in counting ``aclose`` invocations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


_SLOTS = (
    "dept_credentials",
    "admin",
    "webhooks",
    "cancel",
    "repo_sync",
    "po_review",
    "inbound",
    "webhook_v2",
    "webhook_pipeline",
)


@pytest.mark.asyncio
async def test_startup_propagates_pool_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing pool construction propagates with the http client closed.

    The test:

    1. Patches :func:`asyncpg.create_pool` to raise ``OSError``.
    2. Replaces :class:`httpx.AsyncClient` with a counter so the
       handler's cleanup walk is observable.
    3. Enters the lifespan and asserts the exact ``OSError`` re-raises.
    4. Asserts ``http_client.aclose()`` was awaited exactly once.
    5. Asserts no production-wired ``app.state.<slot>`` survived.
    """

    http_client = _CountingHttpClient()

    def _make_http(*args: Any, **kwargs: Any) -> _CountingHttpClient:
        return http_client

    async def _broken_pool(*args: Any, **kwargs: Any) -> Any:
        raise OSError("postgres unreachable")

    monkeypatch.setattr(app_module.httpx, "AsyncClient", _make_http)
    monkeypatch.setattr(app_module.asyncpg, "create_pool", _broken_pool)
    monkeypatch.setenv("AUTH_PROVIDER", "local")

    app = app_module.create_app()

    with pytest.raises(OSError, match="postgres unreachable"):
        async with app_module.lifespan(app):
            # Never reached - the lifespan's ``__aenter__`` raises before
            # ``yield`` runs.
            pytest.fail(
                "lifespan unexpectedly yielded after pool construction "
                "failure; the OSError should have re-raised before the "
                "context body ran."
            )

    # Exactly the one resource constructed before the failing pool
    # (httpx.AsyncClient) had its closer awaited.
    assert http_client.aclose_calls == 1, (
        f"http_client.aclose was called {http_client.aclose_calls} times; "
        "expected exactly 1 (the only resource constructed before the "
        "failing pool)"
    )

    # No production-wired slot was populated - the handler aborted before
    # Phase B and never touched ``app.state.<slot>``.
    for slot in _SLOTS:
        assert getattr(app.state, slot, None) is None, (
            f"production wiring populated app.state.{slot} despite the "
            "startup failure"
        )
