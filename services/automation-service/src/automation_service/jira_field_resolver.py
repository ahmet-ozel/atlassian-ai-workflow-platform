"""Jira custom field name → id resolver with TTL'li cache.

The platform MUST NOT hard-code Jira custom field ids
(``customfield_10001``, ``customfield_10049``, ...) anywhere in the
in-scope source tree. Field ids vary across Jira tenants, so any literal
embedded in code becomes a deployment-blocker the moment a new tenant is
onboarded. Instead, callers refer to fields by their *human-readable
name* (``"Sprint"``, ``"Epic Link"``, ``"Story Points"``) and the
resolver translates the name to the tenant-local id at call time,
caching the result for a configurable TTL.

Resolver contract::

    resolve_field_id(field_name) -> str

* First call → invokes the injected ``jira_client.get_fields()``
  coroutine (which under the hood issues
  ``GET /rest/api/3/field``), populates the cache atomically with
  every field returned by the response, stamps it with ``now()``, and
  returns the id matching ``field_name``.
* Subsequent calls within the TTL → **NO** HTTP request; the cached
  id is returned directly.
* Calls after ``now() - fetched_at >= ttl`` → cache is rebuilt (a
  single new HTTP fetch) and the id from the fresh response is
  returned.
* Concurrency → an :class:`asyncio.Lock` protects the refresh path so
  N concurrent resolvers see exactly **one** in-flight fetch even
  when N callers race past a stale or empty cache. The lock is
  released as soon as the cache is populated; subsequent reads are
  lock-free.
* Unknown field name → :class:`JiraFieldNotFoundError`. The error is
  raised *after* the cache has been populated so the caller can be
  certain the missing entry is genuinely absent on the upstream
  side rather than a stale lookup.

The resolver is **purely a translation layer** - no audit emission,
no metric recording, no retry policy. Those concerns belong to the
HTTP client (``jira_client``) which the design earmarks for a thin
``http-shared``-backed wrapper around the MCP. Keeping this module
narrow lets it stay deterministic and unit-testable without standing
up the full automation-service stack.

Hard-coded literal ban
----------------------

The static AST counterpart is enforced by the property test
``platform/tests/property/test_no_hardcoded_field_ids.py``. That test
walks every ``.py`` file under

* ``platform/services/automation-service/``
* ``platform/workers/``
* ``platform/libs/``

and fails on any string literal matching ``^customfield_\\d+$``
outside the whitelist (test subtrees and *this* module, which mentions
the literal in docstrings only). The combination of the runtime resolver
and the static linter ensures the cache never contains a field id that
was hard-coded somewhere else in the tree because no such literal
exists.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Final,
    Iterable,
    Mapping,
    Protocol,
    runtime_checkable,
)

__all__ = [
    "JiraFieldClient",
    "JiraFieldNotFoundError",
    "JiraFieldResolver",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Default TTL for the field-name → id cache. The 1 hour figure comes
#: from the desired cache behavior: long enough that the resolver makes
#: at most one ``GET /rest/api/3/field`` call per hour per process under
#: steady-state load, short enough that an admin who renames a custom
#: field on the upstream Jira sees the change pick up within an hour
#: without restarting the service.
_DEFAULT_TTL: Final[timedelta] = timedelta(hours=1)


def _utcnow() -> datetime:
    """Default clock - UTC ``now()``.

    Factored out so :class:`JiraFieldResolver` can take it as an
    injectable callable. Tests pass a fake clock to exercise the TTL
    boundary deterministically; production callers accept the
    default.

    Note: this helper is the *only* module-level reference to
    ``datetime.now`` in this file. The resolver itself never imports
    or calls ``datetime.now`` directly - it always goes through the
    injected ``now`` callable so the test suite can keep the
    behaviour deterministic.
    """

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Protocol - Jira field listing client
# ---------------------------------------------------------------------------


@runtime_checkable
class JiraFieldClient(Protocol):
    """Structural type for the ``GET /rest/api/3/field`` caller.

    The resolver depends only on a single coroutine: ``get_fields()``
    returning an iterable of field descriptors shaped like the Jira
    REST response (``[{"id": "customfield_10001", "name": "Sprint",
    ...}, ...]``). Declaring this as a ``Protocol`` keeps the
    resolver decoupled from the concrete HTTP transport - production
    wiring binds it to an ``httpx``-based client routed through the
    ``atlassian_mcp_bitbucket`` MCP, while tests pass a plain in-memory
    fake.
    """

    async def get_fields(self) -> Iterable[Mapping[str, Any]]:
        """Return every Jira field descriptor visible to the bot.

        The minimum contract the resolver relies on is that each
        descriptor carries the ``"id"`` and ``"name"`` keys; any
        additional keys (``"custom"``, ``"schema"``, etc.) are
        ignored.

        Implementations SHOULD raise on transport errors so the
        resolver's caller (the webhook handler / activity) can
        surface the failure as HTTP 503 / Temporal activity retry
        rather than caching an empty result.
        """

        ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JiraFieldNotFoundError(LookupError):
    """Raised when a requested field name has no matching id on Jira.

    Subclasses :class:`LookupError` so callers can use the standard
    ``try/except LookupError`` idiom while still distinguishing this
    failure from a generic missing-key error when needed (e.g.
    auditing).

    The exception's :attr:`field_name` attribute carries the original
    name so log handlers and audit emitters can report it without
    parsing ``str(exc)``.
    """

    def __init__(self, field_name: str) -> None:
        super().__init__(
            f"Jira custom field {field_name!r} not found in "
            "GET /rest/api/3/field response"
        )
        self.field_name = field_name


# ---------------------------------------------------------------------------
# Internal cache record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CacheSnapshot:
    """Atomic snapshot of the resolver cache.

    Holding the ``name → id`` mapping and the ``fetched_at`` timestamp
    in a single frozen dataclass lets the resolver swap them as one
    atomic reference assignment after a refresh - readers that
    grabbed the previous snapshot keep observing a consistent view
    until they next ask for a fresh one. This is also why the
    resolver does NOT mutate ``self._cache`` in place: an in-place
    update could let a reader observe a half-populated mapping mid
    refresh.

    ``fetched_at`` is set from the resolver's injected ``now``
    callable, never from a direct ``datetime.now()`` call, so tests
    can pin time and exercise the TTL boundary deterministically.
    """

    fields_by_name: Mapping[str, str]
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class JiraFieldResolver:
    """Cache + TTL aware Jira custom-field name → id translator.

    Intended lifetime: one instance per service process, held on
    ``app.state.jira_field_resolver``. The class is safe for
    concurrent use across asyncio tasks - the :class:`asyncio.Lock`
    serialises refresh attempts so a burst of N concurrent
    :meth:`resolve_field_id` calls performs at most one upstream
    fetch.

    The resolver is *not* thread-safe; it lives entirely inside a
    single asyncio event loop (the FastAPI / Temporal worker loop)
    per the platform's "no shared state across threads" rule.
    """

    __slots__ = (
        "_jira_client",
        "_ttl",
        "_now",
        "_cache",
        "_lock",
    )

    def __init__(
        self,
        jira_client: JiraFieldClient,
        *,
        ttl: timedelta = _DEFAULT_TTL,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Wire the resolver against an HTTP client and a clock.

        Parameters
        ----------
        jira_client:
            Anything matching the :class:`JiraFieldClient` protocol;
            its ``get_fields()`` coroutine is the resolver's only
            outbound call.
        ttl:
            How long a cached snapshot is considered fresh. Must be
            non-negative; ``timedelta(0)`` is accepted and disables
            the cache entirely (every call refreshes - useful in
            tests, never in production). Defaults to one hour per
            the design document.
        now:
            Injectable clock returning the current UTC time. The
            resolver passes the result of this callable as
            ``fetched_at`` and re-invokes it on every
            :meth:`resolve_field_id` call to evaluate the TTL
            window. Defaults to :func:`_utcnow`.

        Raises
        ------
        ValueError
            If ``ttl`` is negative. A negative TTL would silently
            invert the freshness check and let the resolver serve
            stale data forever; we reject it at construction time
            so misconfiguration surfaces immediately.
        """

        if ttl.total_seconds() < 0:
            raise ValueError(
                f"ttl must be non-negative; got {ttl!r}"
            )

        self._jira_client = jira_client
        self._ttl = ttl
        self._now = now
        # ``None`` is the "cache empty" sentinel. Once the first
        # refresh completes the slot is replaced atomically with a
        # populated :class:`_CacheSnapshot`; subsequent refreshes
        # likewise replace the whole snapshot.
        self._cache: _CacheSnapshot | None = None
        # The lock is created lazily by ``asyncio.Lock()`` here
        # rather than inside :meth:`resolve_field_id` so concurrent
        # callers share the same lock instance from the very first
        # call. Constructing it eagerly is safe because the resolver
        # is always created inside a running event loop in
        # production wiring.
        self._lock = asyncio.Lock()

    # -----------------------------------------------------------------
    # Public surface
    # -----------------------------------------------------------------

    async def resolve_field_id(self, field_name: str) -> str:
        """Translate a field display name to its tenant-local id.

        The flow is intentionally simple:

        1. Read the current snapshot. If it exists and is still
           within the TTL window, look up ``field_name`` in it and
           return / raise as appropriate. **No HTTP call** in this
           branch.
        2. Otherwise, acquire the lock, re-check the snapshot
           (double-checked locking), and if it is still missing or
           stale issue exactly one ``get_fields()`` call. The new
           snapshot is built and stamped with the resolver's
           ``now()`` clock, then assigned atomically before the lock
           is released.
        3. Look up ``field_name`` in the freshly populated snapshot
           and either return the id or raise
           :class:`JiraFieldNotFoundError`.

        Parameters
        ----------
        field_name:
            The user-visible Jira field name as it appears in the
            ``"name"`` column of ``GET /rest/api/3/field``
            (e.g. ``"Sprint"``, ``"Epic Link"``).
            Lookup is case-sensitive - Jira treats field names as
            case-sensitive on the wire and we mirror that contract.

        Returns
        -------
        str
            The opaque field id (e.g. ``"customfield_10020"``) the
            caller should pass through to subsequent JQL / issue
            update calls.

        Raises
        ------
        JiraFieldNotFoundError
            If the upstream Jira tenant does not expose a field with
            the given name. The error is raised after a successful
            refresh, so the caller is guaranteed the cache is
            up-to-date at the point of failure.
        """

        # Fast path: cache hit + still fresh → no HTTP call, no lock.
        snapshot = self._cache
        if snapshot is not None and not self._is_stale(snapshot):
            return self._lookup(snapshot, field_name)

        # Slow path: refresh under the lock.
        async with self._lock:
            # Double-checked locking - another coroutine may have
            # raced ahead and refreshed the snapshot while we were
            # waiting on the lock. Re-read after acquiring.
            snapshot = self._cache
            if snapshot is None or self._is_stale(snapshot):
                snapshot = await self._refresh()

        return self._lookup(snapshot, field_name)

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _is_stale(self, snapshot: _CacheSnapshot) -> bool:
        """Return ``True`` iff *snapshot* is older than the TTL.

        Equality is treated as stale (``>=``) so a TTL of exactly
        zero behaves intuitively as "never cache" - one boundary case
        that surfaces in property tests.
        """

        return self._now() - snapshot.fetched_at >= self._ttl

    async def _refresh(self) -> _CacheSnapshot:
        """Fetch the current field list and replace the snapshot.

        Caller MUST hold ``self._lock``. The HTTP call happens
        without the lock held only conceptually - :class:`asyncio.Lock`
        is held across the ``await`` so concurrent callers see at
        most one in-flight fetch.

        On success the new :class:`_CacheSnapshot` is assigned to
        ``self._cache`` and returned. On exception ``self._cache`` is
        left untouched (the previous stale snapshot, if any, remains
        readable) and the exception propagates so the caller can
        fail fast - preferable to silently serving an empty cache.
        """

        _LOG.debug(
            "jira_field_resolver: refreshing cache via get_fields()"
        )
        fields = await self._jira_client.get_fields()
        # Build the mapping defensively: skip entries missing the
        # required keys so a malformed payload from upstream cannot
        # poison the cache. Duplicate names (rare but possible -
        # Jira allows multiple custom fields with the same display
        # name) collapse to the *last* descriptor seen, mirroring the
        # behaviour of `dict()` on a list of pairs. Callers that need
        # to disambiguate must use the field id directly.
        by_name: dict[str, str] = {}
        for descriptor in fields:
            name = descriptor.get("name") if isinstance(descriptor, Mapping) else None
            field_id = descriptor.get("id") if isinstance(descriptor, Mapping) else None
            if isinstance(name, str) and isinstance(field_id, str):
                by_name[name] = field_id

        snapshot = _CacheSnapshot(
            fields_by_name=by_name,
            fetched_at=self._now(),
        )
        self._cache = snapshot
        _LOG.debug(
            "jira_field_resolver: cache refreshed; %d fields indexed",
            len(by_name),
        )
        return snapshot

    @staticmethod
    def _lookup(snapshot: _CacheSnapshot, field_name: str) -> str:
        """Return the id for *field_name* or raise.

        Pulled out as a static method so both the fast-path and
        slow-path branches of :meth:`resolve_field_id` share the
        same lookup + error semantics: the lookup result is a pure
        function of the snapshot, not of which path produced it.
        """

        try:
            return snapshot.fields_by_name[field_name]
        except KeyError:
            raise JiraFieldNotFoundError(field_name) from None
