"""Unit tests for ``automation_service.jira_field_resolver``.

Validates the runtime contract of :class:`JiraFieldResolver`
(task 4.7, Requirement 3.7, design Property 19):

* First call → exactly one ``get_fields()`` HTTP fetch; cache
  populated.
* Second call inside the TTL → **no** HTTP fetch.
* Third call after the TTL boundary → cache refreshed (one more
  fetch).
* Concurrent callers racing past an empty/stale cache → exactly one
  in-flight fetch (asyncio.Lock invariant).
* Unknown field name → :class:`JiraFieldNotFoundError`.

The Jira HTTP client is replaced by a tiny in-memory fake whose
``get_fields()`` coroutine records every invocation. This avoids any
dependency on httpx / live Jira / the foundation MCP plumbing —
Property 19 is purely about the cache + TTL + lock semantics, all of
which live inside :class:`JiraFieldResolver`.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

# Ensure the automation-service src is importable (mirrors the
# bootstrap used by sibling unit tests in this directory).
#
# Importing :mod:`automation_service.jira_field_resolver` triggers
# ``automation_service.__init__`` which eagerly loads
# ``automation_service.app`` whose top-of-module imports reach for the
# legacy ``from src.config import Settings`` re-export. Two ``sys.path``
# entries are therefore required: ``services/automation-service/src``
# (so the ``automation_service`` package itself resolves) and
# ``services/automation-service`` (so the legacy ``src.*`` modules
# resolve). Mirrors the bootstrap in ``test_app.py`` and
# ``test_credentials.py``.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

from automation_service.jira_field_resolver import (  # noqa: E402
    JiraFieldNotFoundError,
    JiraFieldResolver,
)


# =============================================================================
# Helpers
# =============================================================================


class _FakeJiraClient:
    """In-memory Jira field-list client.

    Implements the structural :class:`JiraFieldClient` Protocol with a
    single ``get_fields()`` coroutine that returns the configured list
    and records the call count. A small ``call_log`` of timestamps is
    also tracked so tests that need to observe ordering can assert on
    it without monkey-patching.
    """

    def __init__(self, fields: Iterable[Mapping[str, Any]]) -> None:
        self._fields: list[Mapping[str, Any]] = list(fields)
        self.call_count: int = 0
        # Async event used by the concurrency test to make every
        # in-flight ``get_fields()`` block until the test releases it,
        # so we can deterministically observe the lock semantics.
        self.gate: asyncio.Event | None = None

    async def get_fields(self) -> Iterable[Mapping[str, Any]]:
        self.call_count += 1
        if self.gate is not None:
            # Wait until the test signals that the in-flight fetch
            # may complete. This lets concurrent callers stack up on
            # the resolver's lock and proves that only one fetch
            # actually runs.
            await self.gate.wait()
        # Return a fresh copy so callers cannot mutate the fixture
        # state by accident.
        return [dict(d) for d in self._fields]

    def set_fields(self, fields: Iterable[Mapping[str, Any]]) -> None:
        """Replace the configured field list (used by the TTL test)."""

        self._fields = list(fields)


class _MutableClock:
    """Manual clock used to drive the TTL boundary deterministically.

    The resolver takes any ``Callable[[], datetime]``; this class
    exposes that callable interface plus an ``advance()`` helper so
    tests can roll time forward without monkey-patching ``datetime``.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


# =============================================================================
# Sample field payload
# =============================================================================


# Minimal viable shape mirroring the Jira ``GET /rest/api/3/field``
# response. Only ``id`` and ``name`` are required by the resolver.
_SAMPLE_FIELDS: tuple[dict[str, str], ...] = (
    {"id": "summary", "name": "Summary"},
    {"id": "customfield_10020", "name": "Sprint"},
    {"id": "customfield_10014", "name": "Epic Link"},
    {"id": "customfield_10016", "name": "Story Points"},
)


_REFRESHED_FIELDS: tuple[dict[str, str], ...] = (
    {"id": "summary", "name": "Summary"},
    # Same name, different id — verifies that a TTL refresh actually
    # serves the new value rather than returning the previously
    # cached one.
    {"id": "customfield_99999", "name": "Sprint"},
)


# =============================================================================
# Tests
# =============================================================================


class TestFirstCallTriggersFetch:
    """First :meth:`resolve_field_id` call → one fetch + populated cache."""

    @pytest.mark.asyncio
    async def test_first_call_invokes_get_fields_once(self) -> None:
        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        result = await resolver.resolve_field_id("Sprint")

        assert result == "customfield_10020"
        assert client.call_count == 1

    @pytest.mark.asyncio
    async def test_first_call_caches_every_field_in_response(self) -> None:
        """Cache is populated atomically with the *full* response.

        The contract (design.md §4.7) requires a single refresh to
        index every field returned by ``get_fields()``. Resolving a
        second name immediately after the first must therefore NOT
        trigger another fetch.
        """

        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        first = await resolver.resolve_field_id("Sprint")
        second = await resolver.resolve_field_id("Epic Link")

        assert first == "customfield_10020"
        assert second == "customfield_10014"
        # Single fetch covers both lookups — the whole field list is
        # cached on the first call.
        assert client.call_count == 1


class TestCacheHitWithinTtl:
    """Repeated calls within the TTL must NOT issue a new fetch."""

    @pytest.mark.asyncio
    async def test_repeated_calls_inside_ttl_skip_http(self) -> None:
        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        await resolver.resolve_field_id("Sprint")
        # Move forward, but stay inside the TTL window.
        clock.advance(timedelta(minutes=30))
        await resolver.resolve_field_id("Sprint")
        clock.advance(timedelta(minutes=29, seconds=59))
        await resolver.resolve_field_id("Story Points")

        assert client.call_count == 1


class TestCacheRefreshAfterTtl:
    """After the TTL boundary, the next call refetches and serves new data."""

    @pytest.mark.asyncio
    async def test_call_after_ttl_triggers_new_fetch(self) -> None:
        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        first = await resolver.resolve_field_id("Sprint")
        assert first == "customfield_10020"
        assert client.call_count == 1

        # Cross the TTL boundary and rotate the upstream field
        # mapping. The resolver MUST observe the new id because the
        # cache is rebuilt from the fresh response.
        clock.advance(timedelta(hours=1))
        client.set_fields(_REFRESHED_FIELDS)

        refreshed = await resolver.resolve_field_id("Sprint")

        assert refreshed == "customfield_99999"
        assert client.call_count == 2

    @pytest.mark.asyncio
    async def test_ttl_boundary_inclusive(self) -> None:
        """``now - fetched_at == ttl`` is treated as stale.

        Property 19 (a–c): the freshness predicate is ``< ttl``,
        i.e. the boundary is exclusive on the fresh side. Equality
        triggers a refresh.
        """

        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        await resolver.resolve_field_id("Sprint")
        # Advance to *exactly* the TTL boundary.
        clock.advance(timedelta(hours=1))
        await resolver.resolve_field_id("Sprint")

        assert client.call_count == 2


class TestConcurrentCallers:
    """Concurrent resolvers must coalesce into one in-flight fetch."""

    @pytest.mark.asyncio
    async def test_concurrent_initial_calls_share_one_fetch(self) -> None:
        """N coroutines racing past an empty cache → 1 fetch.

        The asyncio.Lock inside :class:`JiraFieldResolver` is the
        invariant under test: without it, every concurrent caller
        would observe the cache as empty and issue its own fetch.
        We use the fake client's ``gate`` to block the first
        in-flight fetch until every concurrent caller has stacked up
        on the lock; only then do we release the gate so the single
        fetch can complete.
        """

        client = _FakeJiraClient(_SAMPLE_FIELDS)
        client.gate = asyncio.Event()
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        # Schedule 8 concurrent resolvers. The first one to grab the
        # lock will start the fetch and block on ``gate``; the other
        # seven will wait on the lock.
        tasks = [
            asyncio.create_task(resolver.resolve_field_id("Sprint"))
            for _ in range(8)
        ]
        # Yield to the event loop so all 8 tasks reach the await
        # point inside the resolver before we release the gate.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Release the in-flight fetch and wait for all tasks to
        # complete.
        client.gate.set()
        results = await asyncio.gather(*tasks)

        assert all(r == "customfield_10020" for r in results)
        assert client.call_count == 1


class TestUnknownFieldRaises:
    """Unknown field names raise a typed exception."""

    @pytest.mark.asyncio
    async def test_unknown_field_raises_jira_field_not_found_error(self) -> None:
        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        with pytest.raises(JiraFieldNotFoundError) as exc_info:
            await resolver.resolve_field_id("Definitely Not A Real Field")

        assert exc_info.value.field_name == "Definitely Not A Real Field"
        # The cache *was* refreshed (so the caller knows the absence
        # is real, not stale): the upstream fetch ran exactly once.
        assert client.call_count == 1

    @pytest.mark.asyncio
    async def test_unknown_field_does_not_poison_subsequent_lookups(self) -> None:
        """A miss does not invalidate the cache for known names."""

        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(hours=1), now=clock)

        with pytest.raises(JiraFieldNotFoundError):
            await resolver.resolve_field_id("Bogus")

        # Same TTL window — the next call must hit the cache
        # populated by the failed lookup's refresh.
        result = await resolver.resolve_field_id("Sprint")
        assert result == "customfield_10020"
        assert client.call_count == 1


# =============================================================================
# Constructor validation
# =============================================================================


class TestConstructorValidation:
    """Defensive checks on the resolver constructor."""

    def test_negative_ttl_rejected(self) -> None:
        client = _FakeJiraClient(_SAMPLE_FIELDS)
        with pytest.raises(ValueError, match="ttl must be non-negative"):
            JiraFieldResolver(client, ttl=timedelta(seconds=-1))

    def test_zero_ttl_disables_caching(self) -> None:
        """``ttl=0`` is accepted and forces every call to refresh.

        Useful for tests that need a "never cache" mode without
        special-casing the resolver. Property 19 (a) is preserved:
        the first call still issues a single fetch; the next call
        observes ``now - fetched_at >= 0`` and refetches.
        """

        client = _FakeJiraClient(_SAMPLE_FIELDS)
        clock = _MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        resolver = JiraFieldResolver(client, ttl=timedelta(0), now=clock)

        async def _run() -> None:
            await resolver.resolve_field_id("Sprint")
            await resolver.resolve_field_id("Sprint")

        asyncio.run(_run())

        assert client.call_count == 2
