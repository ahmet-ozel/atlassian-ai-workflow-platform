"""``shared.audit_log`` writer with deferred-queue retry semantics.

This module implements audit writer wiring: the :class:`AuditWriter`
is the single component that writes
:class:`AuditEntry` rows into ``shared.audit_log`` (DDL appended in
the shared schema migration, see ``infra/postgres/50_shared.sql``).

The writer enforces **audit-or-rollback** semantics:

* :meth:`AuditWriter.precheck` issues ``SELECT 1`` on a pooled
  connection. The lifecycle handler calls it **before** invoking
  Compose; if the database is unreachable an
  :class:`AuditUnreachableError` propagates out and the request is
  rejected with ``502`` - no Compose command runs.

* :meth:`AuditWriter.write` performs a single ``INSERT`` and surfaces
  connection errors as :class:`AuditUnreachableError`. It is used for
  the *pre-Compose* "pending" audit row (so the handler can short-
  circuit on DB outage before any side-effect lands).

* :meth:`AuditWriter.write_with_retry` is called **after** Compose
  completes (success or failure). If the row cannot be written the
  entry is pushed onto :attr:`AuditWriter._deferred_queue` and the
  outcome flags ``deferred=True`` so the handler can advertise
  ``audit_write_deferred`` in the response body.

* :meth:`AuditWriter._drain_deferred_queue` is a background task
  that pops entries off the queue and retries them with exponential
  backoff. It is started by :meth:`AuditWriter.start` and stopped
  cleanly by :meth:`AuditWriter.close`.

The ``details_json`` column **must not** contain any Env_Override
*values*. The
:func:`details_with_env_keys` helper builds a payload with only the
*key list* and optional non-secret metadata; it is the canonical way
for callers to construct ``details_json`` payloads so
``tests/property/test_audit_one_to_one.py`` can be confident no
secret value ever reaches the audit table.

Notes on pool-construction
--------------------------
:class:`AuditWriter` accepts an optional ``pool_factory`` so the unit
tests can pass a fake :class:`asyncpg.Pool` without having to monkey-
patch ``asyncpg.create_pool``. The factory's signature is
``async (dsn: str, **kwargs) -> Pool``; the default
:func:`asyncpg.create_pool` matches this exactly.

The writer never imports :mod:`asyncpg` at module-import time when the
caller supplies its own ``pool_factory`` - this keeps the unit test
suite functional even if asyncpg is not installed in the test
environment.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID


# ---------------------------------------------------------------------------
# Public dataclasses & exceptions
# ---------------------------------------------------------------------------


#: Allowed values for ``AuditEntry.action`` - must stay in lock-step with the
#: ``CHECK (action IN (...))`` constraint defined by ``50_shared.sql``.
AuditAction = Literal[
    "start",
    "stop",
    "restart",
    "run_tests",
    "health_streak_alert",
    # Feature-flag start gate.
    # Emitted by ``LifecycleService._check_feature_flags``
    # when a manifest ``feature_flag_dependency`` is disabled. Migration
    # ``003_audit_log_feature_flag_action.sql`` widens the CHECK
    # constraint to include this name.
    "service_start_blocked_feature_flag",
    # Stop + purge_vault profile guard. Emitted by the lifecycle stop
    # endpoint when an operator
    # passes ``purge_vault=true`` while ``settings.deployment_profile``
    # resolves to ``"production"``. The router rejects with 403 and
    # records the attempt for audit. Migration
    # ``004_audit_log_purge_vault_action.sql`` widens the CHECK
    # constraint to include this name.
    "purge_vault_blocked_in_production",
    # Stop + purge_vault Vault purge outcome.
    # ``vault_overrides_purged`` is emitted by
    # :meth:`LifecycleService.stop` when ``purge_vault=true`` is
    # accepted (non-production profile) and the post-stop Vault list +
    # delete sequence completes successfully; the payload carries
    # ``deleted_paths_count``. ``vault_purge_partial_failure`` is the
    # best-effort companion: a Vault list/delete failure during the
    # purge does NOT roll back the (already-successful) Compose stop -
    # the lifecycle service records the partial failure and returns
    # the canonical ``StopResponse`` so the operator's UI flow stays
    # responsive. Migration
    # ``005_audit_log_vault_purge_actions.sql`` widens the CHECK
    # constraint to include both names.
    "vault_overrides_purged",
    "vault_purge_partial_failure",
    # External provider probe audit actions.
    # ``external_provider_probe_failed`` is
    # emitted on every failed probe; ``external_provider_streak_alert``
    # fires once when a provider accumulates 3 consecutive failures
    # (mirrors the ``health_streak_alert`` pattern). Migration
    # ``007_audit_log_external_provider_actions.sql`` widens the CHECK
    # constraint to include both names.
    "external_provider_probe_failed",
    "external_provider_streak_alert",
]

#: Allowed values for ``AuditEntry.outcome`` - must stay in lock-step with the
#: ``CHECK (outcome IN (...))`` constraint defined by ``50_shared.sql``.
AuditOutcome = Literal["success", "failed", "pending"]


@dataclass(frozen=True)
class AuditEntry:
    """Immutable representation of a row destined for ``shared.audit_log``.

    The field set mirrors the table schema exactly. Field order and types
    are validated against the DDL by ``test_audit_one_to_one.py``.

    Critical invariant: ``details_json`` MUST NOT contain Env_Override
    *values*. Use :func:`details_with_env_keys` to construct a compliant
    payload.
    """

    id: UUID
    actor: str
    actor_type: Literal["admin_dashboard_user"]
    service_name: str
    action: AuditAction
    timestamp: datetime
    correlation_id: UUID
    outcome: AuditOutcome
    details_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditWriteOutcome:
    """Result of :meth:`AuditWriter.write_with_retry`.

    ``deferred=True`` means the row could not be written immediately and
    has been pushed onto the deferred queue. The lifecycle handler
    surfaces this flag as ``audit_write_deferred`` in the response body
    so the operator knows the audit row is queued.
    """

    deferred: bool


class AuditUnreachableError(RuntimeError):
    """Raised when the audit log database cannot be reached.

    The lifecycle handler must convert this into a ``502 Bad Gateway``
    response and abort the request **without** running Compose.
    """


# ---------------------------------------------------------------------------
# Pool protocol & helpers
# ---------------------------------------------------------------------------


class _PoolLike(Protocol):
    """Subset of the :class:`asyncpg.Pool` interface used by this module.

    We deliberately depend only on ``acquire`` and ``close`` so the unit
    tests can supply a small fake without importing asyncpg.
    """

    def acquire(self) -> Any:  # actually returns an async context manager
        ...

    async def close(self) -> None:
        ...


PoolFactory = Callable[..., Awaitable[_PoolLike]]


def _default_pool_factory() -> PoolFactory:
    """Return the production pool factory (lazy import of asyncpg).

    Importing asyncpg lazily lets the unit tests run in environments
    where asyncpg is unavailable, as long as they pass a fake factory
    to :class:`AuditWriter`.
    """

    import asyncpg  # type: ignore[import-not-found]

    async def _factory(dsn: str, **kwargs: Any) -> _PoolLike:
        # ``max_inactive_connection_lifetime`` recycles idle connections
        # before the container/NAT layer silently drops a long-idle TCP
        # socket. Without this, a pooled connection that has been idle for
        # ~1h goes half-open; the next ``acquire()`` + query then hangs
        # until a hard timeout, surfacing as a spurious
        # ``audit DB precheck failed: TimeoutError`` on the first
        # Stop/Start the operator issues after a quiet period. A 180s
        # ceiling keeps connections fresh while still pooling within a
        # burst of dashboard actions. ``command_timeout`` bounds any
        # single query so a wedged socket can never hang a request
        # indefinitely. Callers may override either via ``kwargs``.
        kwargs.setdefault("max_inactive_connection_lifetime", 180.0)
        kwargs.setdefault("command_timeout", 10.0)
        return await asyncpg.create_pool(dsn=dsn, **kwargs)

    return _factory


def _is_connection_error(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` represents a database-unreachable failure.

    The list intentionally errs on the side of declaring an exception as
    "connection-level". The lifecycle contract is
    that *anything* preventing the audit row from being durably stored
    must abort the request - the alternative is silently dropping audit
    data, which violates audit-or-rollback.

    We avoid importing :mod:`asyncpg` here so callers that supply a
    fake pool can also raise ``OSError`` / ``ConnectionError`` /
    ``asyncio.TimeoutError`` and have them classified correctly.
    """

    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return True

    # Best-effort detection of asyncpg connection-level errors without a
    # hard import dependency. asyncpg's ``PostgresConnectionError`` and
    # ``InterfaceError`` are the two classes that signal "no usable
    # connection"; we identify them by class name to keep the import
    # graph clean.
    name = type(exc).__name__
    return name in {
        "PostgresConnectionError",
        "ConnectionDoesNotExistError",
        "ConnectionFailureError",
        "CannotConnectNowError",
        "InterfaceError",
        "ConnectionRefusedError",
        "TimeoutError",
    }


# ---------------------------------------------------------------------------
# details_json helper
# ---------------------------------------------------------------------------


def details_with_env_keys(
    env_keys: list[str] | tuple[str, ...],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``details_json`` payload that contains only the env-key *list*.

    Env_Override *values* must never appear in the audit log. This
    helper is the canonical way
    to construct the ``details_json`` field for a Lifecycle_Action: it
    accepts the **list of keys** the operator overrode and an optional
    ``extra`` dict for non-secret metadata (e.g. ``{"reason":
    "compose_failed", "exit_code": 1}``).

    The helper makes a shallow copy of ``extra`` and refuses to allow a
    caller to clobber the ``env_keys`` slot - that field is reserved.

    Parameters
    ----------
    env_keys:
        Iterable of Env_Override key names (LHS of ``KEY=VALUE`` from
        the operator's form submission). Order is preserved as a list.
    extra:
        Optional supplementary metadata. Must NOT contain secret
        values; callers are expected to use this only for things like
        Compose exit codes, error categories, retry counters, etc.

    Returns
    -------
    dict[str, Any]
        ``{"env_keys": [<keys...>], **extra_without_env_keys}``.

    Raises
    ------
    ValueError
        If ``extra`` contains an ``env_keys`` entry (which would
        clobber the canonical key-list field).
    """

    if extra is not None and "env_keys" in extra:
        raise ValueError(
            "extra dict must not contain 'env_keys' - that slot is reserved",
        )

    payload: dict[str, Any] = {"env_keys": list(env_keys)}
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# AuditWriter
# ---------------------------------------------------------------------------


_INSERT_SQL = """
INSERT INTO shared.audit_log (
    id, actor, actor_type, service_name, action, timestamp,
    correlation_id, outcome, details_json
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
""".strip()


class AuditWriter:
    """Pooled writer for ``shared.audit_log`` with deferred-queue retry.

    Lifecycle:

    * Construct the writer (no I/O).
    * Call :meth:`start` once at process startup to open the pool and
      spawn the deferred-queue drainer task.
    * Use :meth:`precheck`, :meth:`write`, :meth:`write_with_retry`
      from request handlers.
    * Call :meth:`close` at shutdown to stop the drainer and close the
      pool.

    The writer is safe to share across coroutines - the underlying
    asyncpg pool handles concurrency and the deferred queue is an
    :class:`asyncio.Queue`.
    """

    #: Minimum delay between consecutive deferred-queue retry attempts
    #: when the database remains unreachable. Tests override this via
    #: the ``retry_initial_delay`` constructor argument.
    DEFAULT_RETRY_INITIAL_DELAY = 1.0

    #: Cap on the exponential backoff used by the drainer. After this
    #: ceiling the drainer keeps retrying at the same cadence.
    DEFAULT_RETRY_MAX_DELAY = 30.0

    #: Upper bound for the ``precheck`` ``SELECT 1`` round-trip. A
    #: half-open pooled connection (idle TCP dropped by Docker/NAT) must
    #: not block the lifecycle request indefinitely; on timeout the
    #: writer recreates the pool once and retries (see :meth:`precheck`).
    PRECHECK_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        *,
        dsn: str,
        deferred_queue: asyncio.Queue[AuditEntry],
        pool_factory: PoolFactory | None = None,
        retry_initial_delay: float | None = None,
        retry_max_delay: float | None = None,
    ) -> None:
        """Initialise the writer (no I/O).

        Parameters
        ----------
        dsn:
            PostgreSQL DSN passed verbatim to ``asyncpg.create_pool``.
        deferred_queue:
            Pre-allocated queue that holds entries which failed to
            write. Sharing the queue with the caller allows tests and
            the lifecycle wiring to inspect / drain it explicitly.
        pool_factory:
            Optional async callable that returns an object compatible
            with :class:`_PoolLike`. Defaults to
            :func:`asyncpg.create_pool`. The factory is invoked with
            ``dsn=<dsn>`` as its sole keyword argument.
        retry_initial_delay / retry_max_delay:
            Tuning knobs for the deferred-queue drainer's exponential
            backoff. Defaults are 1s and 30s respectively. The unit
            tests pass tiny values (e.g. ``0.001``) so the drainer can
            be exercised quickly.
        """

        self._dsn = dsn
        self._deferred_queue = deferred_queue
        self._pool_factory = pool_factory or _default_pool_factory()
        self._retry_initial_delay = (
            retry_initial_delay
            if retry_initial_delay is not None
            else self.DEFAULT_RETRY_INITIAL_DELAY
        )
        self._retry_max_delay = (
            retry_max_delay
            if retry_max_delay is not None
            else self.DEFAULT_RETRY_MAX_DELAY
        )

        self._pool: _PoolLike | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._closed = False

    # ---- Lifecycle ----------------------------------------------------

    async def start(self) -> None:
        """Open the pool and spawn the deferred-queue drainer task.

        Idempotent: a second call is a no-op. The drainer is created
        with :func:`asyncio.create_task` so the writer must be started
        from inside a running event loop.
        """

        if self._closed:
            raise RuntimeError("AuditWriter is closed")
        if self._pool is None:
            self._pool = await self._pool_factory(dsn=self._dsn)
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(
                self._drain_deferred_queue(),
                name="audit-writer-drainer",
            )

    async def close(self) -> None:
        """Stop the drainer and close the pool. Idempotent."""

        self._closed = True
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
            self._drain_task = None
        if self._pool is not None:
            try:
                await self._pool.close()
            finally:
                self._pool = None

    # ---- Public write API --------------------------------------------

    async def precheck(self) -> None:
        """Issue ``SELECT 1`` to verify the audit DB is reachable.

        Called by the lifecycle handler **before** any Compose command
        is invoked. On failure raises
        :class:`AuditUnreachableError` so the caller can return ``502``
        without performing any side-effect.
        """

        pool = self._require_pool()
        try:
            await self._run_select_1(pool)
            return
        except BaseException as exc:
            if not _is_connection_error(exc):
                raise
            first_exc = exc

        # The pooled connection was likely half-open: an idle TCP socket
        # silently dropped by Docker/NAT after a quiet period. The pool
        # object itself outlives uvicorn code-reloads (it is created once
        # in the lifespan), so recycling tuning alone cannot rescue an
        # already-stale pool. Recreate the pool once and retry before
        # declaring the audit DB unreachable - this lets the first
        # Stop/Start after an idle window self-heal instead of returning
        # a spurious 502.
        try:
            await self._reset_pool()
            await self._run_select_1(self._require_pool())
        except BaseException as retry_exc:  # noqa: BLE001
            raise AuditUnreachableError(
                f"audit DB precheck failed: "
                f"{type(retry_exc).__name__}: {retry_exc}",
            ) from retry_exc

    async def _run_select_1(self, pool: _PoolLike) -> None:
        """Run a bounded ``SELECT 1`` against ``pool``.

        Wrapped in :func:`asyncio.wait_for` so a wedged/half-open
        connection can never block the lifecycle request indefinitely;
        a stall surfaces as :class:`asyncio.TimeoutError`, which
        :func:`_is_connection_error` classifies as a connection-level
        failure (triggering the one-shot pool reset above). Combined
        with the pool's ``max_inactive_connection_lifetime`` (which
        recycles idle connections before the socket goes half-open),
        this keeps the dashboard responsive after a quiet period.
        """

        async def _do() -> None:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")

        await asyncio.wait_for(_do(), timeout=self.PRECHECK_TIMEOUT_SECONDS)

    async def _reset_pool(self) -> None:
        """Close and re-open the asyncpg pool (best-effort close).

        Used by :meth:`precheck` to recover from a stale/half-open pool.
        Closing a wedged pool can itself hang, so the close is bounded
        and any failure is swallowed - the important half is opening a
        fresh pool from the factory.
        """

        old_pool = self._pool
        self._pool = None
        if old_pool is not None:
            try:
                await asyncio.wait_for(old_pool.close(), timeout=2.0)
            except BaseException:  # noqa: BLE001 - stale pool close may hang
                pass
        self._pool = await self._pool_factory(dsn=self._dsn)

    async def write(self, entry: AuditEntry) -> None:
        """Insert a single :class:`AuditEntry` into ``shared.audit_log``.

        Used for the pre-Compose "pending" audit row written by the
        lifecycle handler. On a connection-level failure raises
        :class:`AuditUnreachableError` so the caller can roll back the
        request before any Compose command runs.

        Other database errors (e.g. CHECK constraint violations) are
        re-raised verbatim - they indicate a programming error, not a
        transient outage, and the deferred queue would not help.
        """

        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    _INSERT_SQL,
                    entry.id,
                    entry.actor,
                    entry.actor_type,
                    entry.service_name,
                    entry.action,
                    entry.timestamp,
                    entry.correlation_id,
                    entry.outcome,
                    json.dumps(entry.details_json, default=str),
                )
        except BaseException as exc:
            if _is_connection_error(exc):
                raise AuditUnreachableError(
                    f"audit DB write failed: {type(exc).__name__}: {exc}",
                ) from exc
            raise

    async def write_with_retry(self, entry: AuditEntry) -> AuditWriteOutcome:
        """Write the entry, deferring on connection failure.

        Called **after** the Compose lifecycle command has completed.
        On a connection-level failure the entry is
        pushed onto the deferred queue (where the background drainer
        will retry it) and ``AuditWriteOutcome(deferred=True)`` is
        returned, so the caller can attach ``audit_write_deferred`` to
        the response body.

        On non-connection errors (e.g. a malformed entry that violates
        a CHECK constraint) the exception is re-raised: deferring such
        rows would mask programming errors.
        """

        try:
            await self.write(entry)
        except AuditUnreachableError:
            await self._deferred_queue.put(entry)
            return AuditWriteOutcome(deferred=True)
        return AuditWriteOutcome(deferred=False)

    # ---- Internals ----------------------------------------------------

    def _require_pool(self) -> _PoolLike:
        """Return the active pool or raise if the writer was never started."""

        if self._closed:
            raise RuntimeError("AuditWriter is closed")
        if self._pool is None:
            raise RuntimeError(
                "AuditWriter.start() must be awaited before issuing writes",
            )
        return self._pool

    async def _drain_deferred_queue(self) -> None:
        """Background task: drain :attr:`_deferred_queue` with retry/backoff.

        The drainer runs forever (until cancelled by :meth:`close`) and
        applies exponential backoff between retries when the database
        is still unreachable. Successful writes reset the backoff to
        the initial delay.

        The drainer **must not** raise out of the task; uncaught
        exceptions would kill the background loop and silently lose
        future deferred entries. Non-connection errors are surfaced via
        a re-raise from :meth:`write` and we treat them as "drop and
        continue" so a single bad entry does not halt the drainer for
        the rest of the batch.
        """

        delay = self._retry_initial_delay
        while True:
            try:
                entry = await self._deferred_queue.get()
            except asyncio.CancelledError:
                raise

            try:
                await self.write(entry)
            except AuditUnreachableError:
                # Re-queue at the *front* by putting it back; the
                # asyncio.Queue does not have an at-head insert so we
                # accept FIFO loss of order between batches and put it
                # at the tail. Audit ordering is reconstructed by
                # ``timestamp`` at query time.
                await self._deferred_queue.put(entry)
                self._deferred_queue.task_done()
                # Exponential backoff while the DB stays unreachable.
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max_delay)
                continue
            except asyncio.CancelledError:
                # Re-queue so we don't lose the entry on shutdown.
                await self._deferred_queue.put(entry)
                self._deferred_queue.task_done()
                raise
            except Exception:
                # Non-connection error (e.g. CHECK constraint violation).
                # The entry is unrecoverable; mark it done so the
                # drainer can move on. The exception is intentionally
                # swallowed to keep the drainer alive - the lifecycle
                # handler is responsible for emitting structured logs
                # for the original write failure.
                self._deferred_queue.task_done()
                delay = self._retry_initial_delay
                continue

            self._deferred_queue.task_done()
            delay = self._retry_initial_delay


__all__ = (
    "AuditAction",
    "AuditEntry",
    "AuditOutcome",
    "AuditUnreachableError",
    "AuditWriteOutcome",
    "AuditWriter",
    "details_with_env_keys",
)
