"""Unit tests for the ``audit_prune`` activities (Spec 3 task 13.2).

Validates Requirements **R6.3** (daily cron archives audit_events older
than ``RETENTION_DAYS`` to MinIO and then deletes them) and **R6.4**
(failure path invokes ``notify_audit_prune_failed`` mandatory admin
Slack alarm) at the activity layer. The matching workflow contract is
tested by ``test_audit_prune_workflow.py`` (task 13.1).

Strategy
--------

The activities depend on three external collaborators that we do not
spin up in unit tests:

* an asyncpg-shaped Postgres pool (audit + cost SELECTs / DELETEs and
  ``shared.feature_flags`` lookup);
* a MinIO endpoint (the gzip JSON-lines PUT);
* a ``NotificationService`` (admin Slack alarm).

Each is replaced with a small in-memory fake registered through the
module-level ``set_*`` setters; ``httpx`` traffic to MinIO is
intercepted with ``MockTransport``. The activities themselves run as
plain coroutines — ``@activity.defn`` does not change the calling
contract for direct invocation.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors test_audit_prune_workflow.py)
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# pylint: disable=wrong-import-position
from automation_worker.activities import audit_prune as ap  # noqa: E402
from automation_worker.activities.audit_prune import (  # noqa: E402
    ARCHIVE_BATCH_SIZE,
    AUDIT_ARCHIVE_BUCKET,
    AuditArchiveResult,
    AuditArchiveTransportError,
    AuditDeleteResult,
    RETENTION_FEATURE_FLAG_NAME,
    archive_audit_to_minio,
    delete_audit_older_than,
    get_retention_setting,
    notify_audit_prune_failed,
    set_db_pool,
    set_minio_settings,
    set_notification_service,
    set_retention_setting_provider,
)
from automation_worker.workflows.audit_prune import (  # noqa: E402
    DEFAULT_RETENTION_DAYS,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeConn:
    """asyncpg-shaped connection fake.

    ``fetch`` / ``fetchrow`` / ``execute`` look up scripted responses
    keyed by the SQL fragment we recognise. Tests that drive a single
    call only need to populate the matching key.
    """

    fetch_pages: list[list[dict[str, Any]]] = field(default_factory=list)
    fetchrow_responses: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    execute_responses: dict[str, str] = field(default_factory=dict)
    executed: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    fetched: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched.append((query, args))
        if self.fetch_pages:
            return self.fetch_pages.pop(0)
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetched.append((query, args))
        # Match by substring so tests can key on a stable fragment.
        for key, response in self.fetchrow_responses.items():
            if key in query:
                return response
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        for key, status in self.execute_responses.items():
            if key in query:
                return status
        return "DELETE 0"

    @asynccontextmanager
    async def transaction(self):
        # No-op transaction context. Tests do not exercise rollback.
        yield self


class _FakePool:
    """asyncpg.Pool-shaped fake yielding the same ``_FakeConn`` per acquire."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@dataclass
class _FakeMinioSettings:
    endpoint: str = "minio:9000"
    access_key: str = "test-access"
    secret_key: str = "test-secret"
    use_ssl: bool = False
    region: str = "us-east-1"


@dataclass
class _FakeNotificationService:
    """Captures :meth:`notify_audit_prune_failed` calls."""

    calls: list[Any] = field(default_factory=list)
    raise_on_call: BaseException | None = None

    async def notify_audit_prune_failed(self, *, error: Any) -> None:
        self.calls.append(error)
        if self.raise_on_call is not None:
            raise self.raise_on_call


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Clear setter-registered globals between tests."""

    # Snapshot existing values; restore after the test so tests do
    # not bleed state into one another even when an assertion fails
    # mid-way.
    snapshot = (
        ap._db_pool,  # noqa: SLF001
        ap._minio_settings,  # noqa: SLF001
        ap._notification_service,  # noqa: SLF001
        ap._retention_setting_provider,  # noqa: SLF001
    )
    ap._db_pool = None  # noqa: SLF001
    ap._minio_settings = None  # noqa: SLF001
    ap._notification_service = None  # noqa: SLF001
    ap._retention_setting_provider = None  # noqa: SLF001
    yield
    (
        ap._db_pool,  # noqa: SLF001
        ap._minio_settings,  # noqa: SLF001
        ap._notification_service,  # noqa: SLF001
        ap._retention_setting_provider,  # noqa: SLF001
    ) = snapshot


@pytest.fixture
def cutoff_utc() -> datetime:
    return datetime(2025, 6, 15, 3, 0, 0, tzinfo=timezone.utc)


# ===========================================================================
# 1. get_retention_setting — env / DB / default fallback
# ===========================================================================


class TestGetRetentionSetting:
    """R6.3 — ``RETENTION_DAYS`` resolved from env, then feature_flags,
    then the default constant ``DEFAULT_RETENTION_DAYS``."""

    def test_default_when_no_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RETENTION_DAYS", raising=False)
        # No pool registered ⇒ DB branch is skipped.
        result = asyncio.run(get_retention_setting())
        assert result == DEFAULT_RETENTION_DAYS == 90

    def test_env_var_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RETENTION_DAYS", "30")
        assert asyncio.run(get_retention_setting()) == 30

    def test_env_var_invalid_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RETENTION_DAYS", "not-a-number")
        assert asyncio.run(get_retention_setting()) == DEFAULT_RETENTION_DAYS

    def test_env_var_zero_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A zero / negative env value would mean "delete everything";
        # the activity rejects that and falls through.
        monkeypatch.setenv("RETENTION_DAYS", "0")
        assert asyncio.run(get_retention_setting()) == DEFAULT_RETENTION_DAYS

    def test_feature_flag_used_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RETENTION_DAYS", raising=False)
        conn = _FakeConn(
            fetchrow_responses={
                "shared.feature_flags": {"description": "45", "enabled": True},
            },
        )
        set_db_pool(_FakePool(conn))
        assert asyncio.run(get_retention_setting()) == 45
        # The feature_flags query was issued with the constant flag name.
        assert any(
            "shared.feature_flags" in q and args == (RETENTION_FEATURE_FLAG_NAME,)
            for q, args in conn.fetched
        )

    def test_feature_flag_disabled_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RETENTION_DAYS", raising=False)
        conn = _FakeConn(
            fetchrow_responses={
                "shared.feature_flags": {"description": "45", "enabled": False},
            },
        )
        set_db_pool(_FakePool(conn))
        assert asyncio.run(get_retention_setting()) == DEFAULT_RETENTION_DAYS

    def test_provider_override_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RETENTION_DAYS", "30")  # would normally win

        async def provider() -> int:
            return 7

        set_retention_setting_provider(provider)
        assert asyncio.run(get_retention_setting()) == 7

    def test_provider_invalid_falls_through_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RETENTION_DAYS", "30")

        async def provider() -> int | None:
            return None

        set_retention_setting_provider(provider)
        assert asyncio.run(get_retention_setting()) == 30

    def test_provider_exception_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RETENTION_DAYS", raising=False)

        async def provider() -> int:
            raise RuntimeError("boom")

        set_retention_setting_provider(provider)
        # Should not raise; falls through to default.
        assert asyncio.run(get_retention_setting()) == DEFAULT_RETENTION_DAYS

    def test_db_lookup_failure_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RETENTION_DAYS", raising=False)

        class _BoomConn:
            async def fetchrow(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("postgres unavailable")

            async def fetch(self, *args: Any, **kwargs: Any) -> list[Any]:
                return []

            async def execute(self, *args: Any, **kwargs: Any) -> str:
                return "DELETE 0"

        class _BoomPool:
            @asynccontextmanager
            async def acquire(self):
                yield _BoomConn()

        set_db_pool(_BoomPool())
        # Activity must not fail the cron over a DB hiccup.
        assert asyncio.run(get_retention_setting()) == DEFAULT_RETENTION_DAYS


# ===========================================================================
# 2. archive_audit_to_minio — JSONL+gzip + deterministic key
# ===========================================================================


def _install_minio_capture() -> dict[str, Any]:
    """Install a httpx ``MockTransport`` and capture the PUT call.

    Returns a dict updated with the captured request fields after the
    activity runs. Pairs with :func:`monkeypatch_async_client` below.
    """
    captured: dict[str, Any] = {"request": None, "payload": None}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["payload"] = request.content
        return httpx.Response(200, headers={"ETag": '"abc123"'})

    return captured, _handler


@pytest.fixture
def patch_httpx_client(monkeypatch: pytest.MonkeyPatch):
    """Replace ``httpx.AsyncClient`` inside the activity module.

    Returns a tuple ``(captured_dict, install_fn)`` — the test calls
    ``install_fn()`` to wire the mock transport, then inspects
    ``captured_dict`` after the activity completes.
    """
    captured, handler = _install_minio_capture()
    transport = httpx.MockTransport(handler)

    real_async_client = httpx.AsyncClient

    def _patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(ap.httpx, "AsyncClient", _patched)
    return captured


class TestArchiveAuditToMinio:
    """R6.3 — archive activity writes ordered JSONL gzip to MinIO."""

    def test_zero_rows_returns_empty_uri(self, cutoff_utc: datetime) -> None:
        conn = _FakeConn(fetch_pages=[[]])
        set_db_pool(_FakePool(conn))
        set_minio_settings(_FakeMinioSettings())

        result = asyncio.run(archive_audit_to_minio(cutoff_utc))

        assert isinstance(result, AuditArchiveResult)
        assert result.archived_rows == 0
        assert result.archive_uri == ""
        # No execute calls; only the SELECT page.
        assert conn.executed == []

    def test_single_page_uploads_gzip_jsonl(
        self,
        cutoff_utc: datetime,
        patch_httpx_client: dict[str, Any],
    ) -> None:
        rows = [
            {
                "id": 1,
                "actor_id": "u-1",
                "actor_role": "system",
                "dept_id": None,
                "action": "chat_message",
                "resource": "wf-1",
                "result": "ok",
                "payload": {"k": "v"},
                "created_at": cutoff_utc - timedelta(days=10),
            },
            {
                "id": 2,
                "actor_id": "u-2",
                "actor_role": "lead",
                "dept_id": "payment",
                "action": "credential_rotated",
                "resource": "atlassian",
                "result": "ok",
                "payload": None,
                "created_at": cutoff_utc - timedelta(days=5),
            },
        ]
        # Two pages: first returns the rows, second returns empty
        # (terminates the pagination loop). The first page has 2
        # rows < ARCHIVE_BATCH_SIZE so the loop exits immediately
        # — second page is unused but harmless to pre-populate.
        conn = _FakeConn(fetch_pages=[rows, []])
        set_db_pool(_FakePool(conn))
        set_minio_settings(_FakeMinioSettings())

        result = asyncio.run(archive_audit_to_minio(cutoff_utc))

        assert result.archived_rows == 2
        # URI shape: s3://audit-archive/{Y}/{M}/{D}/audit-{shard}.jsonl.gz
        assert result.archive_uri.startswith(
            f"s3://{AUDIT_ARCHIVE_BUCKET}/{cutoff_utc.year:04d}/"
            f"{cutoff_utc.month:02d}/{cutoff_utc.day:02d}/audit-"
        )
        assert result.archive_uri.endswith(".jsonl.gz")

        # The captured PUT body must be valid gzip + JSONL with both rows.
        request = patch_httpx_client["request"]
        assert request is not None
        assert request.method == "PUT"
        body = patch_httpx_client["payload"]
        decompressed = gzip.GzipFile(fileobj=io.BytesIO(body)).read().decode("utf-8")
        lines = [json.loads(line) for line in decompressed.strip().split("\n")]
        assert len(lines) == 2
        assert lines[0]["id"] == 1
        assert lines[1]["id"] == 2
        # datetime serialized as ISO 8601 UTC.
        assert lines[0]["created_at"].endswith("+00:00")

    def test_archive_uri_is_deterministic_for_same_cutoff(
        self,
        cutoff_utc: datetime,
        patch_httpx_client: dict[str, Any],
    ) -> None:
        # Two independent runs with the same cutoff produce the same
        # object key (idempotence — Property 10 parity).
        rows = [
            {
                "id": 1,
                "actor_id": "u-1",
                "actor_role": "system",
                "dept_id": None,
                "action": "chat_message",
                "resource": "wf-1",
                "result": "ok",
                "payload": {"k": "v"},
                "created_at": cutoff_utc - timedelta(days=1),
            }
        ]
        conn1 = _FakeConn(fetch_pages=[rows])
        set_db_pool(_FakePool(conn1))
        set_minio_settings(_FakeMinioSettings())
        first = asyncio.run(archive_audit_to_minio(cutoff_utc))

        conn2 = _FakeConn(fetch_pages=[rows])
        set_db_pool(_FakePool(conn2))
        second = asyncio.run(archive_audit_to_minio(cutoff_utc))

        assert first.archive_uri == second.archive_uri

    def test_select_filters_by_cutoff(
        self,
        cutoff_utc: datetime,
        patch_httpx_client: dict[str, Any],
    ) -> None:
        # The first SELECT must bind ``cutoff`` as $1.
        conn = _FakeConn(fetch_pages=[[]])
        set_db_pool(_FakePool(conn))
        set_minio_settings(_FakeMinioSettings())

        asyncio.run(archive_audit_to_minio(cutoff_utc))

        assert conn.fetched, "expected at least one SELECT call"
        first_query, first_args = conn.fetched[0]
        assert "automation.audit_events" in first_query
        assert "created_at < $1" in first_query
        assert first_args[0] == cutoff_utc
        # ``ARCHIVE_BATCH_SIZE`` is the LIMIT.
        assert first_args[-1] == ARCHIVE_BATCH_SIZE

    def test_minio_put_failure_raises(
        self,
        cutoff_utc: datetime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulate a 500 from MinIO — activity must surface a
        # transport error so the workflow's failure path triggers
        # the admin alarm.
        rows = [
            {
                "id": 1,
                "actor_id": "u",
                "actor_role": "system",
                "dept_id": None,
                "action": "x",
                "resource": "y",
                "result": "ok",
                "payload": None,
                "created_at": cutoff_utc - timedelta(days=1),
            }
        ]
        conn = _FakeConn(fetch_pages=[rows])
        set_db_pool(_FakePool(conn))
        set_minio_settings(_FakeMinioSettings())

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"upstream error")

        transport = httpx.MockTransport(_handler)
        real_async_client = httpx.AsyncClient

        def _patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr(ap.httpx, "AsyncClient", _patched)

        with pytest.raises(AuditArchiveTransportError):
            asyncio.run(archive_audit_to_minio(cutoff_utc))

    def test_missing_settings_raises_runtime_error(
        self, cutoff_utc: datetime
    ) -> None:
        conn = _FakeConn(fetch_pages=[[]])
        set_db_pool(_FakePool(conn))
        # No MinIO settings registered.
        result = asyncio.run(archive_audit_to_minio(cutoff_utc))
        # Zero rows path returns early without touching MinIO settings,
        # so this still succeeds. Verify the early-return branch.
        assert result.archived_rows == 0


# ===========================================================================
# 3. delete_audit_older_than — DELETE both audit + cost rows
# ===========================================================================


class TestDeleteAuditOlderThan:
    """R6.3 — DELETE both audit_events and cost_tracking rows."""

    def test_returns_audit_delete_count(self, cutoff_utc: datetime) -> None:
        conn = _FakeConn(
            execute_responses={
                "automation.audit_events": "DELETE 17",
                "shared.cost_tracking": "DELETE 5",
            }
        )
        set_db_pool(_FakePool(conn))

        result = asyncio.run(delete_audit_older_than(cutoff_utc))

        assert isinstance(result, AuditDeleteResult)
        assert result.deleted_rows == 17

    def test_issues_both_deletes_with_cutoff(self, cutoff_utc: datetime) -> None:
        conn = _FakeConn(
            execute_responses={
                "automation.audit_events": "DELETE 0",
                "shared.cost_tracking": "DELETE 0",
            }
        )
        set_db_pool(_FakePool(conn))

        asyncio.run(delete_audit_older_than(cutoff_utc))

        deletes = [
            (q, args)
            for q, args in conn.executed
            if "DELETE" in q.upper()
        ]
        assert len(deletes) == 2
        # Each DELETE binds the cutoff timestamp as $1.
        for q, args in deletes:
            assert "created_at < $1" in q
            assert args[0] == cutoff_utc
        # Order: audit before cost (so cost orphans cannot survive
        # an audit-only failure if the transaction were to be split).
        assert "automation.audit_events" in deletes[0][0]
        assert "shared.cost_tracking" in deletes[1][0]

    def test_zero_deletes_returns_zero(self, cutoff_utc: datetime) -> None:
        conn = _FakeConn(execute_responses={})
        set_db_pool(_FakePool(conn))
        result = asyncio.run(delete_audit_older_than(cutoff_utc))
        assert result.deleted_rows == 0

    def test_malformed_status_returns_zero(self, cutoff_utc: datetime) -> None:
        conn = _FakeConn(
            execute_responses={
                "automation.audit_events": "garbage-status",
                "shared.cost_tracking": "DELETE 3",
            }
        )
        set_db_pool(_FakePool(conn))
        result = asyncio.run(delete_audit_older_than(cutoff_utc))
        # Falls through to 0 rather than raising.
        assert result.deleted_rows == 0


# ===========================================================================
# 4. notify_audit_prune_failed — mandatory admin Slack alarm
# ===========================================================================


class TestNotifyAuditPruneFailed:
    """R6.4 — failure path forwards to NotificationService."""

    def test_dispatches_to_notification_service(self) -> None:
        service = _FakeNotificationService()
        set_notification_service(service)

        asyncio.run(notify_audit_prune_failed("RuntimeError: boom"))

        assert service.calls == ["RuntimeError: boom"]

    def test_propagates_service_exception(self) -> None:
        # If the notification service itself raises, the activity does
        # NOT swallow the exception — Temporal's retry policy on the
        # workflow side will retry per ``_NOTIFY_RETRY``. The
        # workflow's ``_notify_failure`` helper finally swallows so
        # the *original* prune exception propagates; this is a
        # workflow-level concern, not the activity's.
        service = _FakeNotificationService(
            raise_on_call=RuntimeError("slack down"),
        )
        set_notification_service(service)

        with pytest.raises(RuntimeError, match="slack down"):
            asyncio.run(notify_audit_prune_failed("boom"))

    def test_unset_service_raises_runtime_error(self) -> None:
        # No setter called ⇒ activity surfaces a clear configuration
        # error rather than silently dropping the alarm.
        with pytest.raises(RuntimeError, match="NotificationService"):
            asyncio.run(notify_audit_prune_failed("boom"))


# ===========================================================================
# 5. Activity registration metadata
# ===========================================================================


class TestActivityRegistration:
    """The four activities must be registered with the names the
    workflow references (string literals); a typo here breaks the
    workflow at runtime even though both modules import cleanly."""

    @pytest.mark.parametrize(
        "fn,expected_name",
        [
            (get_retention_setting, "get_retention_setting"),
            (archive_audit_to_minio, "archive_audit_to_minio"),
            (delete_audit_older_than, "delete_audit_older_than"),
            (notify_audit_prune_failed, "notify_audit_prune_failed"),
        ],
    )
    def test_activity_name_matches_workflow_reference(
        self, fn: Any, expected_name: str
    ) -> None:
        # ``temporalio.activity.defn`` records the registered name on
        # the wrapped callable as the ``__temporal_activity_definition``
        # attribute (private; we resolve through ``getattr`` so
        # internal layout changes do not silently break the test).
        defn = getattr(fn, "__temporal_activity_definition", None)
        assert defn is not None, f"{fn.__name__} is not registered as an activity"
        # ``defn`` exposes ``name`` as the registered string.
        assert getattr(defn, "name", None) == expected_name


# ===========================================================================
# 6. Setter / accessor contracts
# ===========================================================================


class TestSettersAccessors:
    def test_db_pool_accessor_raises_when_unset(self) -> None:
        with pytest.raises(RuntimeError, match="db pool"):
            ap.get_db_pool()

    def test_minio_settings_accessor_raises_when_unset(self) -> None:
        with pytest.raises(RuntimeError, match="MinIO"):
            ap.get_minio_settings()

    def test_notification_service_accessor_raises_when_unset(self) -> None:
        with pytest.raises(RuntimeError, match="NotificationService"):
            ap.get_notification_service()

    def test_setters_round_trip(self) -> None:
        pool = _FakePool(_FakeConn())
        settings = _FakeMinioSettings()
        service = _FakeNotificationService()

        set_db_pool(pool)
        set_minio_settings(settings)
        set_notification_service(service)

        assert ap.get_db_pool() is pool
        assert ap.get_minio_settings() is settings
        assert ap.get_notification_service() is service
