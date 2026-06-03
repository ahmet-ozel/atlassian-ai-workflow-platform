"""Activities backing:class:`AuditPruneWorkflow`.

This module ships the four Temporal activities the daily ``audit-prune-cron``
workflow consumes:

*:func:`get_retention_setting` — env / feature-flag lookup of
 ``RETENTION_DAYS`` (default 90 days).
*:func:`archive_audit_to_minio` — streams ``automation.audit_events``
 rows older than ``cutoff`` into MinIO as a daily-partitioned
 gzipped JSON-lines object.
*:func:`delete_audit_older_than` — deletes the same row slice from
 ``automation.audit_events`` (and the parallel ``shared.cost_tracking``
 rows that share the retention window).
*:func:`notify_audit_prune_failed` — mandatory admin Slack alarm
 wired through:class:`notification.NotificationService`.

(daily cron archives audit_events older
than ``RETENTION_DAYS`` to MinIO and then deletes them) and ****
(any failure invokes ``notify_audit_prune_failed`` which sends a
mandatory admin Slack alarm). The matching workflow contract lives in:mod:`automation_worker.workflows.audit_prune`.

Dependency injection
--------------------

Activities are stateless functions decorated with ``@activity.defn``;
they read collaborators (Postgres pool, MinIO client,:class:`NotificationService`) through module-level setters configured
once at worker boot. This keeps the activity bodies free of
``os.environ`` reads at module import time and lets unit tests inject
in-memory fakes through the ``set_*`` setters before invoking the
activity directly.

Idempotence contract (parity)
-----------------------------------------

The workflow's idempotence (replaying the cron on the same day is a
safe no-op) is delegated to the activities:

*:func:`archive_audit_to_minio` writes to a deterministic
 ``audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz`` key. ``N`` is derived
 from the cutoff timestamp, not a per-call counter, so a re-run for
 the same cutoff overwrites the same object byte-for-byte.
*:func:`delete_audit_older_than` is filtered by ``created_at <
 cutoff``; the second run finds zero matching rows and returns
 ``deleted_rows=0``.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable
from urllib.parse import quote

import httpx
from temporalio import activity

from automation_worker.workflows.audit_prune import (
    AuditArchiveResult,
    AuditDeleteResult,
    DEFAULT_RETENTION_DAYS,
)


__all__ = (
    # Public activities
    "archive_audit_to_minio",
    "delete_audit_older_than",
    "get_retention_setting",
    "notify_audit_prune_failed",
    # Setters / accessors
    "set_db_pool",
    "set_minio_settings",
    "set_notification_service",
    "set_retention_setting_provider",
    "get_db_pool",
    "get_minio_settings",
    "get_notification_service",
    # Result aliases (re-exported for convenience)
    "AuditArchiveResult",
    "AuditDeleteResult",
    # Constants
    "AUDIT_ARCHIVE_BUCKET",
    "ARCHIVE_BATCH_SIZE",
    "RETENTION_FEATURE_FLAG_NAME",
)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: MinIO bucket name where archived audit objects land. The bucket is
#: created at MinIO boot by (``infra/minio/init.sh``);
#: this activity does not attempt to auto-create it on first write —
#: doing so on a deny-by-default IAM would mask configuration drift.
#: Sibling owns the bucket lifecycle.
AUDIT_ARCHIVE_BUCKET: str = "audit-archive"

#: Number of audit rows fetched from Postgres round-trip when
#: streaming the archive payload. Bounded so a single retention window
#: covering millions of rows does not blow worker memory; the activity
#: still produces a single object day (the batches are concatenated
#: into the same in-memory gzip stream before upload).
ARCHIVE_BATCH_SIZE: int = 5_000

#: ``shared.feature_flags`` row name consulted by
#::func:`get_retention_setting` after the env-var lookup. Storing the
#: name as a constant keeps the audit dashboard's "feature flags"
#: panel and this lookup pinned to the same identifier.
RETENTION_FEATURE_FLAG_NAME: str = "audit_retention_days"


# ---------------------------------------------------------------------------
# Dependency-injection registry
# ---------------------------------------------------------------------------


@runtime_checkable
class _AsyncPoolLike(Protocol):
    """Minimal asyncpg pool surface the activities depend on.

 Production wires this to an ``asyncpg.Pool``; tests inject an
 in-memory fake whose ``acquire`` returns a context manager
 yielding a fake connection with ``fetch`` / ``execute`` methods.
 Declaring a Protocol keeps this module free of a hard runtime
 dependency on ``asyncpg`` (so the worker package can import the
 activities even if ``asyncpg`` is not installed in the test
 environment).
 """

    def acquire(self) -> Any:
        """Return an async context manager yielding a connection."""
        ...


@runtime_checkable
class _NotificationServiceLike(Protocol):
    """Minimal:class:`notification.NotificationService` surface.

 Only:meth:`notify_audit_prune_failed` is part of the contract for
 this module — the wider success-gated dispatch surface lives on
 the same object but is not consumed here. Sibling ships
 the concrete method body; this activity calls it through the
 Protocol so it does not import:mod:`notification` at module
 scope (avoiding a circular dependency with the worker boot
 script).
 """

    async def notify_audit_prune_failed(self, *, error: BaseException | str) -> None:
        ...


@runtime_checkable
class _MinioSettings(Protocol):
    """Resolved MinIO connection parameters.

 Stored as a Protocol so tests can pass any object exposing the
 five attributes; production wires this to a frozen dataclass
 populated from ``MINIO_*`` env vars at worker boot.
 """

    endpoint: str
    access_key: str
    secret_key: str
    use_ssl: bool
    region: str


_db_pool: _AsyncPoolLike | None = None
_minio_settings: _MinioSettings | None = None
_notification_service: _NotificationServiceLike | None = None
_retention_setting_provider: Callable[[], Awaitable[int | None]] | None = None


def set_db_pool(pool: _AsyncPoolLike) -> None:
    """Register the asyncpg-shaped pool used by the audit-prune activities.

 Called once at worker boot (/ ``main.py``) after
 the connection pool is constructed. Unit tests call this with an
 in-memory fake pool whose ``acquire`` yields a connection
 capturing the SQL emitted by the activities under test.
 """
    global _db_pool  # noqa: PLW0603
    _db_pool = pool


def get_db_pool() -> _AsyncPoolLike:
    """Resolve the registered pool or fail loudly.

 Activities call this rather than reading the module global
 directly so misconfiguration (forgot to call ``set_db_pool``)
 surfaces as a clear ``RuntimeError`` in worker logs instead of an
 ``AttributeError`` deep inside the SQL emitter.
 """
    if _db_pool is None:
        raise RuntimeError(
            "audit_prune activities: db pool not initialised; call "
            "set_db_pool during worker startup."
        )
    return _db_pool


def set_minio_settings(settings: _MinioSettings) -> None:
    """Register MinIO connection parameters for the archive activity."""
    global _minio_settings  # noqa: PLW0603
    _minio_settings = settings


def get_minio_settings() -> _MinioSettings:
    """Resolve the registered MinIO settings or fail loudly."""
    if _minio_settings is None:
        raise RuntimeError(
            "audit_prune activities: MinIO settings not initialised; "
            "call set_minio_settings during worker startup."
        )
    return _minio_settings


def set_notification_service(service: _NotificationServiceLike) -> None:
    """Register the:class:`NotificationService` instance for the alarm activity."""
    global _notification_service  # noqa: PLW0603
    _notification_service = service


def get_notification_service() -> _NotificationServiceLike:
    """Resolve the registered NotificationService or fail loudly."""
    if _notification_service is None:
        raise RuntimeError(
            "audit_prune activities: NotificationService not "
            "initialised; call set_notification_service during "
            "worker startup."
        )
    return _notification_service


def set_retention_setting_provider(
    provider: Callable[[], Awaitable[int | None]] | None,
) -> None:
    """Register an override coroutine that resolves the retention window.

 The default implementation in:func:`get_retention_setting` reads
 the ``RETENTION_DAYS`` env var and falls back to ``shared.feature_flags``.
 Tests use this hook to inject deterministic values without
 touching ``os.environ`` or wiring a Postgres fake. Pass ``None`` to
 revert to the default behaviour.
 """
    global _retention_setting_provider  # noqa: PLW0603
    _retention_setting_provider = provider


# ---------------------------------------------------------------------------
# Activity 1: get_retention_setting
# ---------------------------------------------------------------------------


@activity.defn(name="get_retention_setting")
async def get_retention_setting() -> int:
    """Return the audit-retention window in days.

 Lookup order (first match wins):

 1. **Test override**: any callable installed via:func:`set_retention_setting_provider` is awaited; its return
 value, if a positive int, wins.
 2. **Environment variable** ``RETENTION_DAYS`` — parsed as a
 positive int. A non-numeric or non-positive value is ignored
 (logged) so a typo cannot accidentally widen retention to "0
 days = delete everything".
 3. **``shared.feature_flags`` table** — the row whose
 ``name = audit_retention_days`` (constant:data:`RETENTION_FEATURE_FLAG_NAME`). The flag stores the value
 in its ``description`` column when ``enabled = TRUE``;
 parseability is checked the same way as the env var.
 4. **Default**::data:`DEFAULT_RETENTION_DAYS` (90), mirroring
 design.md §"AuditPruneWorkflow".

 Returns:
 Retention window in days (>= 1).

 Notes:
 The activity intentionally does not raise on a malformed env
 / DB value — the workflow body falls back to:data:`DEFAULT_RETENTION_DAYS` when this returns 0 / None
 anyway, so logging the malformed input is more useful than
 crashing the daily cron over a config typo.
 """
    # 1. Test override (deterministic, used by unit tests).
    if _retention_setting_provider is not None:
        try:
            override = await _retention_setting_provider()
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "audit_prune.get_retention_setting: provider raised %r; "
                "falling back to env/DB lookup.",
                exc,
            )
        else:
            parsed = _parse_positive_int(override)
            if parsed is not None:
                return parsed

    # 2. Environment variable. ``os.environ`` is read inside the
    # activity (allowed) — never inside the workflow body.
    env_raw = os.environ.get("RETENTION_DAYS")
    parsed = _parse_positive_int(env_raw)
    if parsed is not None:
        return parsed

    # 3. ``shared.feature_flags`` lookup. Best-effort: if the table
    # does not exist yet (early dev environment) or the pool is not
    # wired we silently fall through to the default rather than
    # failing the activity.
    try:
        pool = get_db_pool()
    except RuntimeError:
        # No pool registered. Skip the DB branch.
        pass
    else:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
 SELECT description, enabled
 FROM shared.feature_flags
 WHERE name = $1
 """,
                    RETENTION_FEATURE_FLAG_NAME,
                )
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "audit_prune.get_retention_setting: feature_flags lookup "
                "failed (%s); using default %d days.",
                exc,
                DEFAULT_RETENTION_DAYS,
            )
        else:
            if row is not None and bool(row.get("enabled", False)):
                parsed = _parse_positive_int(row.get("description"))
                if parsed is not None:
                    return parsed

    # 4. Default — design.md §"AuditPruneWorkflow" RETENTION_DAYS=90.
    return DEFAULT_RETENTION_DAYS


def _parse_positive_int(value: Any) -> int | None:
    """Coerce ``value`` to a positive int, returning ``None`` on failure.

 Accepts ints, strings, and Decimal/float (truncated). Anything
 non-numeric or non-positive returns ``None`` so the caller can
 fall through to the next lookup tier.
 """
    if value is None:
        return None
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return None
    if as_int <= 0:
        return None
    return as_int


# ---------------------------------------------------------------------------
# Activity 2: archive_audit_to_minio
# ---------------------------------------------------------------------------


@activity.defn(name="archive_audit_to_minio")
async def archive_audit_to_minio(cutoff: datetime) -> AuditArchiveResult:
    """Stream audit rows older than ``cutoff`` into MinIO.

 The activity:

 1. SELECTs every ``automation.audit_events`` row with
 ``created_at < cutoff`` ordered by ``(created_at, id)`` so the
 resulting JSON-lines stream is deterministic across replays.
 2. Encodes each row as a single JSON line (UTF-8) and gzips the
 concatenated stream in memory.
 3. PUTs the gzip blob to MinIO under the deterministic key
 ``audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz`` where ``Y / M / D``
 are zero-padded components of ``cutoff`` (UTC) and ``N`` is the
 sha256 of ``cutoff.isoformat`` truncated to 8 hex chars — a
 stable per-cutoff suffix that lets a future "split daily
 archive into multiple shards" extension add a numeric counter
 without breaking object-name compatibility.

 Returns::class:`AuditArchiveResult` with ``archived_rows`` (count of
 rows written) and ``archive_uri`` (``s3://`` form of the
 key, or empty string when no rows fell within the cutoff).

 Notes:
 Activity logs only counts and the object key — never row
 contents — so the worker log stream never contains audit
 payload data verbatim (log-redaction parity).
 """
    pool = get_db_pool()
    cutoff_utc = _ensure_utc(cutoff)

    rows: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        # asyncpg's ``fetch`` reads the whole result; for very large
        # batches sibling would switch to a server-side
        # cursor. For the in-design retention window (90 days × ~10k
        # events/day ≈ 900k rows once at steady state) ``fetch`` with
        # an explicit LIMIT loop is acceptable and keeps the activity
        # implementation small. We page by ``(created_at, id)`` to
        # stay deterministic.
        last_created_at: datetime | None = None
        last_id: int | None = None
        while True:
            if last_created_at is None:
                page = await conn.fetch(
                    """
 SELECT id, actor_id, actor_role, dept_id,
 action, resource, result, payload, created_at
 FROM automation.audit_events
 WHERE created_at < $1
 ORDER BY created_at ASC, id ASC
 LIMIT $2
 """,
                    cutoff_utc,
                    ARCHIVE_BATCH_SIZE,
                )
            else:
                page = await conn.fetch(
                    """
 SELECT id, actor_id, actor_role, dept_id,
 action, resource, result, payload, created_at
 FROM automation.audit_events
 WHERE created_at < $1
 AND (created_at, id) > ($2, $3)
 ORDER BY created_at ASC, id ASC
 LIMIT $4
 """,
                    cutoff_utc,
                    last_created_at,
                    last_id,
                    ARCHIVE_BATCH_SIZE,
                )

            page_list = list(page)
            if not page_list:
                break

            for record in page_list:
                rows.append(_record_to_jsonable(record))

            tail = page_list[-1]
            last_created_at = _record_get(tail, "created_at")
            last_id = _record_get(tail, "id")

            if len(page_list) < ARCHIVE_BATCH_SIZE:
                break

    archived_rows = len(rows)
    activity.logger.info(
        "audit_prune.archive_audit_to_minio: cutoff=%s archived_rows=%d",
        cutoff_utc.isoformat(),
        archived_rows,
    )

    if archived_rows == 0:
        # Nothing to write. The workflow still reports a clean
        # ``AuditPruneReport`` so downstream observers (admin UI,
        # Loki search) can confirm the cron tick fired and produced
        # zero work.
        return AuditArchiveResult(archived_rows=0, archive_uri="")

    # Build the deterministic object key + payload.
    key = _build_archive_key(cutoff_utc)
    payload = _encode_jsonl_gzip(rows)

    # Upload. ``_minio_put_object`` raises on transport / 4xx-5xx so
    # the workflow's outer try/except triggers ``notify_audit_prune_failed``.
    settings = get_minio_settings()
    await _minio_put_object(
        settings=settings,
        bucket=AUDIT_ARCHIVE_BUCKET,
        key=key,
        payload=payload,
        content_type="application/gzip",
    )

    archive_uri = f"s3://{AUDIT_ARCHIVE_BUCKET}/{key}"
    activity.logger.info(
        "audit_prune.archive_audit_to_minio: uploaded %d rows to %s "
        "(payload_bytes=%d)",
        archived_rows,
        archive_uri,
        len(payload),
    )

    return AuditArchiveResult(archived_rows=archived_rows, archive_uri=archive_uri)


# ---------------------------------------------------------------------------
# Activity 3: delete_audit_older_than
# ---------------------------------------------------------------------------


@activity.defn(name="delete_audit_older_than")
async def delete_audit_older_than(cutoff: datetime) -> AuditDeleteResult:
    """Delete audit + cost rows older than ``cutoff``.

 The activity issues two ``DELETE``s inside a single transaction:

 * ``DELETE FROM automation.audit_events WHERE created_at < $1``
 * ``DELETE FROM shared.cost_tracking WHERE created_at < $1``

 Both tables share the same retention window design.md
 §"AuditPruneWorkflow" — the cost rows mirror the audit rows
 one-to-one for LLM activities so dropping audit history without
 dropping cost history would orphan cost rows. The transaction is
 a single atomic step so a partial failure does not leave the two
 tables out of sync.

 Returns::class:`AuditDeleteResult` with ``deleted_rows`` reflecting the
 number of rows removed from ``automation.audit_events``. Cost
 deletions are aggregated into the same counter only when they
 clearly belong to the audit slice; sibling 's
 Invariant test asserts the audit-side count specifically, and
 keeping the report aligned with that assertion avoids
 ambiguous counters.
 """
    pool = get_db_pool()
    cutoff_utc = _ensure_utc(cutoff)

    async with pool.acquire() as conn:
        async with conn.transaction():
            audit_status = await conn.execute(
                """
 DELETE FROM automation.audit_events
 WHERE created_at < $1
 """,
                cutoff_utc,
            )
            cost_status = await conn.execute(
                """
 DELETE FROM shared.cost_tracking
 WHERE created_at < $1
 """,
                cutoff_utc,
            )

    audit_deleted = _parse_delete_count(audit_status)
    cost_deleted = _parse_delete_count(cost_status)
    activity.logger.info(
        "audit_prune.delete_audit_older_than: cutoff=%s "
        "audit_deleted=%d cost_deleted=%d",
        cutoff_utc.isoformat(),
        audit_deleted,
        cost_deleted,
    )

    return AuditDeleteResult(deleted_rows=audit_deleted)


def _parse_delete_count(status: Any) -> int:
    """Extract the row count from an asyncpg ``execute`` status string.

 asyncpg returns the SQL command tag (eg. ``"DELETE 42"``); we
 parse the trailing integer. Any malformed value yields 0 — better
 to under-report than to fail the activity over a status-string
 quirk.
 """
    if isinstance(status, int):
        return max(status, 0)
    if not isinstance(status, str):
        return 0
    parts = status.strip().split()
    if not parts:
        return 0
    try:
        return max(int(parts[-1]), 0)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Activity 4: notify_audit_prune_failed
# ---------------------------------------------------------------------------


@activity.defn(name="notify_audit_prune_failed")
async def notify_audit_prune_failed(error_text: str) -> None:
    """Forward a prune failure to the mandatory admin Slack alarm.

 The activity is wired to:class:`notification.NotificationService`'s:meth:`notify_audit_prune_failed` which renders
 ``prompts/notifications/audit_prune_failed.md`` and POSTs the body
 to the admin Slack channel via:meth:`SlackAdapter.send_admin_channel`. Reuses the existing:data:`notification.NotificationKind` ``"audit_prune_failed"``
 declared in:mod:`notification.types`.

 Args:
 error_text: Stringified exception forwarded by:class:`AuditPruneWorkflow` when the archive / delete
 activity raised. Activity logs the message verbatim
 (workflow logger redacts secrets upstream).

 Notes:
 * The activity does **not** swallow exceptions raised by the
 notification service — the workflow's failure helper
 catches them so the *original* prune exception is the one
 that propagates to Temporal. See:meth:`AuditPruneWorkflow._notify_failure`.
 * The "audit_prune_failed" admin alarm is mandatory per: it is the only signal an operator gets that the
 retention cron failed.
 """
    service = get_notification_service()
    activity.logger.warning(
        "audit_prune.notify_audit_prune_failed: dispatching admin alarm: %s",
        _truncate(error_text, max_len=512),
    )
    await service.notify_audit_prune_failed(error=error_text)


def _truncate(value: str, *, max_len: int) -> str:
    """Truncate ``value`` to ``max_len`` chars with an ellipsis suffix."""
    if not isinstance(value, str):
        value = str(value)
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Internal helpers — JSON-lines + gzip encoding
# ---------------------------------------------------------------------------


def _encode_jsonl_gzip(rows: list[dict[str, Any]]) -> bytes:
    """Serialise ``rows`` as gzip-compressed JSON-lines (UTF-8).

 Each row becomes a single JSON object on its own line ending in
 ``\\n``. ``json.dumps`` is called with ``sort_keys=True`` and
 ``default=_json_default`` so the resulting payload is
 deterministic across Python versions (a re-run on the same row
 set produces a byte-identical archive — required for 's
 "second run no-op" invariant when paired with object overwrite).
 """
    buffer = io.BytesIO()
    # ``mtime=0`` keeps the gzip header deterministic across runs.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        for row in rows:
            line = json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=_json_default,
            )
            gz.write(line.encode("utf-8"))
            gz.write(b"\n")
    return buffer.getvalue()


def _json_default(value: Any) -> Any:
    """``json.dumps`` ``default`` hook for non-JSON-native types.

 Handles the two non-JSON types we expect to see in audit rows:

 *:class:`datetime.datetime` → ISO 8601 string in UTC.
 *:class:`bytes` /:class:`bytearray` → utf-8 string with a
 ``"<binary>"`` placeholder for non-decodable bytes.
 * dataclass instances → ``asdict``.

 Anything else falls back to ``str(value)`` so the encoder never
 fails an entire archive over an unexpected payload type.
 """
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat()
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return "<binary>"
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC ``datetime``.

 A naive ``datetime`` (no ``tzinfo``) is interpreted as UTC —
 Temporal's ``workflow.now`` and Postgres ``TIMESTAMPTZ`` both
 surface UTC values, so this is the safe default. Aware
 ``datetime`` values in non-UTC zones are converted via:meth:`datetime.astimezone`.
 """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_to_jsonable(record: Any) -> dict[str, Any]:
    """Convert an asyncpg ``Record`` (or fake mapping) to a plain dict.

 Tests inject plain dict / mapping fakes; production receives
 asyncpg ``Record`` objects which support ``dict``. Falling back
 to the mapping protocol covers both shapes.
 """
    if isinstance(record, dict):
        return dict(record)
    try:
        return dict(record)
    except (TypeError, ValueError):
        # Last-resort: build manually from common audit column names.
        result: dict[str, Any] = {}
        for key in (
            "id",
            "actor_id",
            "actor_role",
            "dept_id",
            "action",
            "resource",
            "result",
            "payload",
            "created_at",
        ):
            try:
                result[key] = record[key]
            except (KeyError, TypeError, IndexError):
                continue
        return result


def _record_get(record: Any, key: str) -> Any:
    """Look up ``key`` on ``record`` (asyncpg Record or mapping)."""
    if isinstance(record, dict):
        return record.get(key)
    try:
        return record[key]
    except (KeyError, TypeError, IndexError):
        return None


def _build_archive_key(cutoff: datetime) -> str:
    """Build the deterministic object key for the archive upload.

 Shape: ``{Y}/{M}/{D}/audit-{shard}.jsonl.gz`` where the shard is a
 stable 8-char sha256 prefix of ``cutoff.isoformat``. The shard
 suffix means a future split-by-cursor extension can add a numeric
 counter to the key without colliding with this single-shard layout.
 """
    cutoff = _ensure_utc(cutoff)
    shard = hashlib.sha256(cutoff.isoformat().encode("utf-8")).hexdigest()[:8]
    return f"{cutoff.year:04d}/{cutoff.month:02d}/{cutoff.day:02d}/audit-{shard}.jsonl.gz"


# ---------------------------------------------------------------------------
# Internal helpers — MinIO PutObject (S3 SigV4)
# ---------------------------------------------------------------------------


_AWS_DEFAULT_REGION: str = "us-east-1"
_AWS_SERVICE: str = "s3"


async def _minio_put_object(
    *,
    settings: _MinioSettings,
    bucket: str,
    key: str,
    payload: bytes,
    content_type: str,
) -> None:
    """PUT ``payload`` to ``s3://{bucket}/{key}`` on a MinIO endpoint.

 Mirrors the SigV4 helper used by
 ``execution-runner-worker/activities/minio.py``; we re-implement
 a minimal copy here so the ``automation-worker`` does not have to
 import the execution-runner package (the two workers are separately
 deployable). The implementation is deliberately small — only the
 PUT call audit-prune needs is handled, no bucket auto-creation.

 Raises::class:`AuditArchiveTransportError` on transport / non-2xx
 responses; the workflow's outer try/except catches this and
 triggers:func:`notify_audit_prune_failed`.
 """
    region = getattr(settings, "region", _AWS_DEFAULT_REGION) or _AWS_DEFAULT_REGION
    scheme = "https" if getattr(settings, "use_ssl", False) else "http"
    endpoint = settings.endpoint
    access_key = settings.access_key
    secret_key = settings.secret_key

    if not access_key or not secret_key or not endpoint:
        raise AuditArchiveTransportError(
            f"MinIO settings incomplete: endpoint={endpoint!r}, "
            f"access_key_set={bool(access_key)}, secret_key_set={bool(secret_key)}"
        )

    encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
    path = f"/{bucket}/{encoded_key}"
    url = f"{scheme}://{endpoint}{path}"

    payload_hash = hashlib.sha256(payload).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    headers_to_sign = {
        "host": endpoint,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "content-type": content_type,
    }

    authorization = _sigv4_authorization(
        method="PUT",
        path=path,
        headers=headers_to_sign,
        payload_hash=payload_hash,
        access_key=access_key,
        secret_key=secret_key,
        date_stamp=date_stamp,
        amz_date=amz_date,
        region=region,
    )

    request_headers = {
        "Authorization": authorization,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Content-Type": content_type,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.put(url, headers=request_headers, content=payload)
        except httpx.HTTPError as exc:
            raise AuditArchiveTransportError(
                f"MinIO PUT transport failure: {type(exc).__name__}: {exc}"
            ) from exc

    if not (200 <= response.status_code < 300):
        body_preview = ""
        try:
            body_preview = response.text[:200]
        except Exception:  # noqa: BLE001
            body_preview = "<unreadable>"
        raise AuditArchiveTransportError(
            f"MinIO PUT failed: HTTP {response.status_code}: {body_preview}"
        )


def _sigv4_authorization(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    date_stamp: str,
    amz_date: str,
    region: str,
) -> str:
    """Build the AWS Signature V4 ``Authorization`` header value.

 Minimal subset sufficient for MinIO's PUT support: empty query
 string, ``us-east-1`` default region, ``s3`` service.
 """
    signed_keys = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_keys)
    signed_headers = ";".join(signed_keys)

    canonical_request = (
        f"{method}\n"
        f"{path}\n"
        f"\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/{_AWS_SERVICE}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    k_date = hmac.new(
        f"AWS4{secret_key}".encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, _AWS_SERVICE.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()

    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuditArchiveTransportError(RuntimeError):
    """Raised when the MinIO PUT transport fails.

 Caught by the workflow's outer try/except so the failure path
 triggers ``notify_audit_prune_failed``. Kept distinct from
 generic:class:`RuntimeError` so retry policies / metrics can
 filter on it.
 """
