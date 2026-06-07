"""Unit tests for ``automation_service.app._close_quietly``.

During lifespan shutdown the helper must close every owned resource on
a best-effort basis. A failing close on one resource MUST NOT block the
next one from running, MUST NOT propagate out of the lifespan
``__aexit__``, and SHALL be observable via a single WARNING log line
that names the resource and carries the full traceback.

Three branches are exercised here:

* **Success** - ``coro_factory()`` returns an awaitable that completes
  normally. The helper awaits it, returns ``None`` and emits no log
  records.
* **Awaitable raises** - ``coro_factory()`` returns an awaitable that
  raises :class:`RuntimeError`. The helper catches the exception
  inside its ``try/except``, logs at WARNING with the resource name
  and ``exc_info=True``, and never re-raises.
* **``coro_factory`` itself raises** - the *factory* call raises
  :class:`AttributeError` (the documented "no-close" case for the
  :class:`TemporalClient` wrapper). Because the call lives inside
  the helper's ``try`` block, the exception is captured by the same
  WARNING log path and never propagates out - shutdown continues.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Make the in-tree ``src`` directory importable so ``automation_service``
# resolves under both focused and root-level pytest invocations. This
# mirrors the bootstrap used by the sibling lifespan / app tests.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


from automation_service.app import _close_quietly  # noqa: E402


# The helper logs through ``logging.getLogger(__name__)`` of
# ``automation_service.app``. Pin the logger name once so the
# ``caplog`` assertions below stay decoupled from any future move
# of the helper into a sibling module.
_LOGGER_NAME = "automation_service.app"


class TestCloseQuietlySuccess:
    """Awaitable returned by ``coro_factory`` completes normally."""

    @pytest.mark.asyncio
    async def test_awaits_coro_and_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The helper awaits the coroutine and emits no log records.

        Captures the success branch: a closer that returns cleanly
        must leave the WARNING log path untouched so an operator's
        log noise reflects only real shutdown failures.
        """

        calls: list[str] = []

        async def _do_close() -> None:
            calls.append("closed")

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = await _close_quietly("pool", _do_close)

        assert result is None
        # The factory was invoked exactly once and the awaitable it
        # produced was actually awaited (the marker append happens
        # inside the coroutine body, not at factory call time).
        assert calls == ["closed"]
        # No WARNING records on the success path.
        assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []

    @pytest.mark.asyncio
    async def test_does_not_call_factory_twice(self) -> None:
        """``coro_factory`` is invoked exactly once on success."""

        invocations = 0

        async def _do_close() -> None:
            return None

        def _factory() -> "object":
            nonlocal invocations
            invocations += 1
            return _do_close()

        await _close_quietly("http_client", _factory)

        assert invocations == 1


class TestCloseQuietlyAwaitableRaises:
    """The awaitable returned by ``coro_factory`` raises ``RuntimeError``."""

    @pytest.mark.asyncio
    async def test_runtime_error_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A RuntimeError from ``await`` is captured, logged, never raised.

        If a single resource raises during shutdown, the service still
        continues closing the remaining resources. Combined with the
        lifespan handler's sequential calls to ``_close_quietly`` in
        reverse construction order, swallow-and-log here is what lets
        the next closer run.
        """

        async def _broken_close() -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            # The call must NOT raise - that is the whole contract.
            result = await _close_quietly("http_client", _broken_close)

        assert result is None

        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert len(records) == 1, records

        record = records[0]
        assert record.levelno == logging.WARNING
        # The log message must name the resource so an operator can
        # correlate the failure with the design's reverse-shutdown
        # order ("temporal" → "http_client" → "pool").
        assert "http_client" in record.getMessage()
        # ``exc_info=True`` was requested by the helper, so the
        # captured record carries the original ``RuntimeError`` for
        # downstream traceback rendering.
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError
        assert isinstance(record.exc_info[1], RuntimeError)
        assert str(record.exc_info[1]) == "boom"


class TestCloseQuietlyFactoryRaises:
    """The ``coro_factory`` call itself raises ``AttributeError``.

    This is the documented "no-close" path for the
    :class:`TemporalClient` wrapper and any other resource whose
    handle simply does not expose a ``close``/``aclose`` method. The
    spec requires the AttributeError raised by the *factory* call
    (i.e. attribute lookup on a missing method) to be captured
    inside the helper's ``try`` block - so that the lifespan keeps
    walking its reverse-shutdown list instead of unwinding through
    ``finally``.
    """

    @pytest.mark.asyncio
    async def test_attribute_error_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _missing_close() -> "object":
            # Simulates ``temporal.close`` resolving to a real method
            # whose invocation immediately fails with AttributeError -
            # the same shape :class:`AttributeError` would take if the
            # caller had passed a wrapper that does not expose a
            # close coroutine factory at all.
            raise AttributeError("'TemporalClient' object has no attribute 'close'")

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = await _close_quietly("temporal", _missing_close)

        assert result is None

        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert len(records) == 1, records

        record = records[0]
        assert record.levelno == logging.WARNING
        assert "temporal" in record.getMessage()
        # The AttributeError raised at the ``coro_factory()`` call site
        # must be captured by the same ``except Exception`` branch and
        # surfaced via ``exc_info`` for operator diagnostics.
        assert record.exc_info is not None
        assert record.exc_info[0] is AttributeError
        assert isinstance(record.exc_info[1], AttributeError)
