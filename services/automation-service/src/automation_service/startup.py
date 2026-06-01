"""Startup-time validation hooks for the automation-service.

This module owns the boot-time invariants the design document
classifies under "fail-fast" — checks that must surface as a process
exit before any HTTP request lands. Each function is async so it can
be awaited from the FastAPI lifespan handler with the same DB pool
the rest of the service uses.

Currently shipped (task 6.2):

* :func:`validate_account_id_consistency` — when a department row
  carries both a manually configured ``account_id`` and an
  ``auto_fetched_account_id`` returned by the read probe, the two
  values MUST be byte-equal. Otherwise the service refuses to start
  and the error message lists **both** values verbatim so an
  operator can diagnose without consulting logs (R5.7, R5.8).

Added by platform-real-usage-gaps task 6.1:

* :func:`auto_probe_missing_account_ids` — best-effort startup hook
  that queries ``automation.department_bots`` for rows where
  ``account_id IS NULL OR account_id = ''``, runs an Atlassian read
  probe for each, and upserts the resolved ``account_id`` back into
  the table. Failures are audited but never block service startup
  (R6.1, R6.4, R6.5).

The module exposes a small dataclass
:class:`AccountIdMismatch` so callers can render structured errors
in their own logging surface; in the canonical wiring the FastAPI
lifespan handler raises :class:`AccountIdMismatchError` directly,
which causes the application factory to exit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence

__all__ = [
    "AccountIdMismatch",
    "AccountIdMismatchError",
    "AutoProbeResult",
    "BotIdentityProber",
    "DeptAccountIdRow",
    "DeptBotMissingRow",
    "MissingAccountIdReader",
    "AccountIdWriter",
    "AuditSink",
    "auto_probe_missing_account_ids",
    "compare_account_ids",
    "find_account_id_mismatches",
    "format_mismatch_error_message",
    "get_probe_cache",
    "validate_account_id_consistency",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-level probe cache (R6.4) — 5 minute TTL
# ---------------------------------------------------------------------------

_PROBE_CACHE_TTL_SECONDS: int = 300  # 5 minutes

# Cache structure: {(dept_id, service): (timestamp, account_id_or_None)}
_probe_cache: dict[tuple[str, str], tuple[float, str | None]] = {}


def get_probe_cache() -> dict[tuple[str, str], tuple[float, str | None]]:
    """Return the module-level probe cache (for testing/inspection)."""
    return _probe_cache


_CACHE_MISS: object = object()  # Module-level sentinel for cache misses


def _cache_get(dept_id: str, service: str) -> str | None | object:
    """Return cached account_id or :data:`_CACHE_MISS` if not cached / expired."""
    key = (dept_id, service)
    entry = _probe_cache.get(key)
    if entry is None:
        return _CACHE_MISS
    ts, account_id = entry
    if time.time() - ts > _PROBE_CACHE_TTL_SECONDS:
        del _probe_cache[key]
        return _CACHE_MISS
    return account_id


def _cache_put(dept_id: str, service: str, account_id: str | None) -> None:
    """Store a probe result in the cache with current timestamp."""
    _probe_cache[(dept_id, service)] = (time.time(), account_id)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeptAccountIdRow:
    """Row shape this module consumes from any source.

    The DB / config-file readers translate their native row format
    into this small projection so the comparison logic stays free of
    persistence-layer concerns. The validation layer only needs the
    four fields below.

    Attributes:
        dept_id: Department id (``departments.id`` column).
        service: Atlassian surface — ``"jira"``, ``"bitbucket"``,
            ``"confluence"``.
        manual_account_id: ``account_id`` value taken from
            ``departments.json`` / ``automation.department_bots``.
            ``None`` when the operator did not pre-configure one.
        auto_fetched_account_id: ``account_id`` returned by the read
            probe (``ProbeResult.auto_fetched_account_id``). ``None``
            when no probe has been run yet for this row.
    """

    dept_id: str
    service: str
    manual_account_id: str | None
    auto_fetched_account_id: str | None


@dataclass(frozen=True, slots=True)
class AccountIdMismatch:
    """A single ``(dept_id, service)`` row whose two values disagree."""

    dept_id: str
    service: str
    manual: str
    auto_fetched: str

    def message(self) -> str:
        """Return the canonical fail-fast line for this mismatch.

        The format is fixed so log parsers and runbooks can match
        against it without a regex over the surrounding context.
        ``repr()`` is used on each value so tabs / newlines are
        rendered visibly and a leading / trailing space cannot hide
        the discrepancy.
        """

        return (
            f"account_id mismatch for dept={self.dept_id!r} "
            f"service={self.service!r}: "
            f"manual={self.manual!r}, auto_fetched={self.auto_fetched!r}"
        )


class AccountIdMismatchError(RuntimeError):
    """Raised by :func:`validate_account_id_consistency` on any mismatch.

    The exception carries the full list of mismatches so the caller
    can render every offending row in a single error frame; the
    default ``str(exc)`` lists the canonical message for each.
    """

    def __init__(self, mismatches: Sequence[AccountIdMismatch]) -> None:
        self.mismatches = tuple(mismatches)
        super().__init__(format_mismatch_error_message(self.mismatches))


# ---------------------------------------------------------------------------
# Pure comparison primitives
# ---------------------------------------------------------------------------


def compare_account_ids(
    manual: str | None,
    auto_fetched: str | None,
) -> bool:
    """Return ``True`` when the two values are *consistent*.

    Consistency rules (R5.7):

    * Both ``None`` → consistent (no validation possible yet).
    * Either side ``None`` → consistent (only one source has spoken
      so far; the next probe / next config push will fill the other).
    * Both non-``None`` → byte-equal comparison; mismatch otherwise.

    The function is intentionally tiny so the callers (admin
    endpoint, startup hook, probe runner) all agree on the same
    notion of "consistent". Whitespace is **not** stripped — leading
    or trailing spaces in either value indicate operator error and
    must surface as a mismatch.
    """

    if manual is None or auto_fetched is None:
        return True
    return manual == auto_fetched


def find_account_id_mismatches(
    rows: Iterable[DeptAccountIdRow],
) -> tuple[AccountIdMismatch, ...]:
    """Return every row whose manual / auto-fetched ids disagree.

    Args:
        rows: Iterable of :class:`DeptAccountIdRow`. Order is
            preserved in the output so callers can stably render
            the list in their error frames.

    Returns:
        Tuple of :class:`AccountIdMismatch` — empty when every row
        is consistent.
    """

    mismatches: list[AccountIdMismatch] = []
    for row in rows:
        if compare_account_ids(row.manual_account_id, row.auto_fetched_account_id):
            continue
        # Both sides present and differ.
        assert row.manual_account_id is not None
        assert row.auto_fetched_account_id is not None
        mismatches.append(
            AccountIdMismatch(
                dept_id=row.dept_id,
                service=row.service,
                manual=row.manual_account_id,
                auto_fetched=row.auto_fetched_account_id,
            )
        )
    return tuple(mismatches)


def format_mismatch_error_message(
    mismatches: Sequence[AccountIdMismatch],
) -> str:
    """Render the fail-fast error message a caller may pass to ``stderr``."""

    if not mismatches:
        return "no account_id mismatches"
    header = (
        f"refusing to start: {len(mismatches)} dept account_id "
        f"mismatch(es) detected (manual vs auto-fetched):"
    )
    body = "\n".join(f"  - {m.message()}" for m in mismatches)
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Source readers (Protocol — keeps the validator backend-agnostic)
# ---------------------------------------------------------------------------


class DeptAccountIdReader(Protocol):
    """Anything that yields :class:`DeptAccountIdRow` instances.

    The canonical implementation reads from
    ``automation.department_bots`` joined against the latest probe
    result for each ``(dept_id, service)`` pair. Tests inject an
    in-memory list. Both forms satisfy the protocol below.
    """

    async def list_rows(self) -> Sequence[DeptAccountIdRow]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def validate_account_id_consistency(
    reader: DeptAccountIdReader,
) -> None:
    """Walk every dept_bot row; raise on any manual/auto-fetched mismatch.

    Called by the FastAPI lifespan handler before the service starts
    accepting traffic (R5.7 / R5.8). On the first inconsistency the
    helper raises :class:`AccountIdMismatchError` whose message lists
    every offending row — supplying both values verbatim so the
    operator can diff without round-tripping logs.

    Args:
        reader: An object satisfying :class:`DeptAccountIdReader`.
            Production wiring binds a thin asyncpg-backed reader; the
            startup property test injects an in-memory fake.

    Raises:
        AccountIdMismatchError: At least one row has both values set
            and they differ.
    """

    rows = await reader.list_rows()
    mismatches = find_account_id_mismatches(rows)
    if mismatches:
        raise AccountIdMismatchError(mismatches)
    _LOG.info(
        "startup.account_id_consistency_ok rows=%d",
        len(rows) if hasattr(rows, "__len__") else -1,
    )


# ---------------------------------------------------------------------------
# Auto-probe: fill missing bot account_ids at startup (R6.1, R6.4, R6.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeptBotMissingRow:
    """A department_bots row whose account_id is NULL or empty.

    Attributes:
        dept_id: Department identifier.
        service: Atlassian surface — ``"jira"``, ``"bitbucket"``,
            ``"confluence"``.
        credential_ref: Vault path for the credential
            (e.g. ``vault:atlassian/<dept>/<service>``).
        username: Bot username from the department_bots row.
    """

    dept_id: str
    service: str
    credential_ref: str
    username: str


@dataclass(frozen=True, slots=True)
class AutoProbeResult:
    """Outcome of a single auto-probe attempt during startup.

    Attributes:
        dept_id: Department identifier.
        service: Atlassian surface.
        success: Whether the probe resolved an account_id.
        account_id: Resolved account_id (None on failure).
        error: Error description on failure (None on success).
    """

    dept_id: str
    service: str
    success: bool
    account_id: str | None = None
    error: str | None = None


class BotIdentityProber(Protocol):
    """Protocol for the component that resolves a bot's account_id.

    The production implementation issues an Atlassian read probe
    (``/myself`` for Jira/Confluence, ``/user`` for Bitbucket) and
    returns the ``accountId`` from the response. Tests inject a fake
    that returns deterministic values.
    """

    async def probe_account_id(
        self,
        dept_id: str,
        service: str,
        credential_ref: str,
        username: str,
    ) -> str | None:
        """Probe Atlassian and return the account_id, or None on failure.

        The implementation MUST NOT raise — failures are signalled by
        returning ``None``. Any exception that escapes is caught by
        the caller and treated as a probe failure.
        """
        ...  # pragma: no cover - protocol


class MissingAccountIdReader(Protocol):
    """Reads department_bots rows where account_id is NULL or empty."""

    async def list_missing(self) -> Sequence[DeptBotMissingRow]:
        """Return rows needing an account_id probe."""
        ...  # pragma: no cover - protocol


class AccountIdWriter(Protocol):
    """Writes the resolved account_id back to department_bots."""

    async def upsert_account_id(
        self,
        dept_id: str,
        service: str,
        account_id: str,
    ) -> None:
        """UPDATE automation.department_bots SET account_id = ...

        The implementation should be idempotent — writing the same
        value twice is a no-op.
        """
        ...  # pragma: no cover - protocol


class AuditSink(Protocol):
    """Minimal audit interface for the auto-probe hook."""

    async def write_auto_probe_audit(
        self,
        action: str,
        dept_id: str,
        service: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Write an audit event for the auto-probe outcome."""
        ...  # pragma: no cover - protocol


async def auto_probe_missing_account_ids(
    *,
    reader: MissingAccountIdReader,
    prober: BotIdentityProber,
    writer: AccountIdWriter,
    audit: AuditSink,
) -> list[AutoProbeResult]:
    """Best-effort startup hook: probe and fill missing bot account_ids.

    Called by the FastAPI lifespan handler after
    :func:`validate_account_id_consistency`. For every
    ``automation.department_bots`` row where ``account_id IS NULL OR
    account_id = ''``, this function:

    1. Checks the process-level cache — if the same ``(dept_id,
       service)`` was probed within the last 5 minutes, skips.
    2. Calls the prober to resolve the account_id via Atlassian API.
    3. On success: writes the account_id to the DB, caches the result,
       and emits ``bot_account_id_auto_filled`` audit.
    4. On failure: caches the failure (to avoid re-probe), emits
       ``bot_account_id_probe_failed`` audit.

    This function **never raises** — all errors are caught and logged.
    Service startup is never blocked by probe failures (R6.1 best-effort).

    .. important:: **Invariant (foundation R7.2 — idempotent config)**

       Probe results are written **only** to the
       ``automation.department_bot_identity`` Postgres table via the
       :class:`AccountIdWriter` protocol.  ``config/departments.json``
       is **never** modified by this function — existing
       ``account_id: ""`` entries in the JSON file remain untouched.
       This preserves the idempotent-config invariant: the JSON file
       is the operator's static declaration; runtime-resolved values
       live exclusively in the database.

    Args:
        reader: Provides the list of department_bots rows with missing
            account_ids.
        prober: Resolves account_ids via Atlassian API calls.
        writer: Persists resolved account_ids back to the DB.
        audit: Writes audit events for probe outcomes.

    Returns:
        List of :class:`AutoProbeResult` for each row processed
        (useful for testing and logging).
    """

    _MISS = _CACHE_MISS
    results: list[AutoProbeResult] = []

    try:
        rows = await reader.list_missing()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "startup.auto_probe.read_failed err=%s",
            type(exc).__name__,
        )
        return results

    _LOG.info(
        "startup.auto_probe.start rows_to_probe=%d",
        len(rows),
    )

    for row in rows:
        # Check cache — skip if recently probed (R6.4)
        cached = _cache_get(row.dept_id, row.service)
        if cached is not _MISS:
            _LOG.debug(
                "startup.auto_probe.cache_hit dept=%s service=%s",
                row.dept_id,
                row.service,
            )
            continue

        # Run the probe
        try:
            account_id = await prober.probe_account_id(
                dept_id=row.dept_id,
                service=row.service,
                credential_ref=row.credential_ref,
                username=row.username,
            )
        except Exception as exc:  # noqa: BLE001
            account_id = None
            error_type = type(exc).__name__
            _LOG.warning(
                "startup.auto_probe.exception dept=%s service=%s err=%s",
                row.dept_id,
                row.service,
                error_type,
            )

        if account_id:
            # Success path — upsert + audit
            try:
                await writer.upsert_account_id(
                    dept_id=row.dept_id,
                    service=row.service,
                    account_id=account_id,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "startup.auto_probe.write_failed dept=%s service=%s err=%s",
                    row.dept_id,
                    row.service,
                    type(exc).__name__,
                )
                # Treat write failure as probe failure
                _cache_put(row.dept_id, row.service, None)
                results.append(AutoProbeResult(
                    dept_id=row.dept_id,
                    service=row.service,
                    success=False,
                    error=f"db_write_failed: {type(exc).__name__}",
                ))
                try:
                    await audit.write_auto_probe_audit(
                        action="bot_account_id_probe_failed",
                        dept_id=row.dept_id,
                        service=row.service,
                        payload={
                            "dept_id": row.dept_id,
                            "service": row.service,
                            "error_type": f"db_write_failed: {type(exc).__name__}",
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue

            _cache_put(row.dept_id, row.service, account_id)
            results.append(AutoProbeResult(
                dept_id=row.dept_id,
                service=row.service,
                success=True,
                account_id=account_id,
            ))

            try:
                await audit.write_auto_probe_audit(
                    action="bot_account_id_auto_filled",
                    dept_id=row.dept_id,
                    service=row.service,
                    payload={
                        "dept_id": row.dept_id,
                        "service": row.service,
                        "resolved_account_id": account_id,
                    },
                )
            except Exception:  # noqa: BLE001
                pass  # Audit failure must not block startup

            _LOG.info(
                "startup.auto_probe.filled dept=%s service=%s account_id=%s",
                row.dept_id,
                row.service,
                account_id[:8] + "..." if len(account_id) > 8 else account_id,
            )
        else:
            # Failure path — audit + cache
            error_msg = "probe_returned_none"
            _cache_put(row.dept_id, row.service, None)
            results.append(AutoProbeResult(
                dept_id=row.dept_id,
                service=row.service,
                success=False,
                error=error_msg,
            ))

            try:
                await audit.write_auto_probe_audit(
                    action="bot_account_id_probe_failed",
                    dept_id=row.dept_id,
                    service=row.service,
                    payload={
                        "dept_id": row.dept_id,
                        "service": row.service,
                        "error_type": error_msg,
                    },
                )
            except Exception:  # noqa: BLE001
                pass  # Audit failure must not block startup

            _LOG.info(
                "startup.auto_probe.failed dept=%s service=%s",
                row.dept_id,
                row.service,
            )

    succeeded = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    _LOG.info(
        "startup.auto_probe.done total=%d succeeded=%d failed=%d",
        len(results),
        succeeded,
        failed,
    )

    return results
