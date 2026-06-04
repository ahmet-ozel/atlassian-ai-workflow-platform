"""Smoke tests for ``llm-provider-management`` spec wiring.

Exercises the public surface end-to-end with in-memory fakes so the
service / router / schema integration is verified without a live
Postgres + Vault + LLM upstream.  Focused on the contracts the
property suite later doubles down on.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest


# Path bootstrap — mirrors the existing audit-related unit tests.
_API_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _API_ROOT.parents[1]

for _path in (
    _API_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "auth-shared" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
    _PLATFORM_ROOT / "libs" / "observability" / "src",
):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


from src.llm_providers.connection_tester import (  # noqa: E402
    ConnectionTester,
    TestRequest,
)
from src.llm_providers.masking import mask  # noqa: E402
from src.llm_providers.schemas import (  # noqa: E402
    ConnectionTestResult,
    LLMProviderConfigDTO,
    OpenAICreate,
    ProviderUpdate,
    UnsavedTestRequest,
)
from src.llm_providers.service import (  # noqa: E402
    ProviderInactive,
    ProviderInUse,
    ProviderNotFound,
    ProviderService,
    VaultDeleteFailed,
    VaultWriteFailed,
    _ProviderCreateInput,
)
from src.lifecycle.vault_client import VaultWriteError  # noqa: E402


# ---------------------------------------------------------------------------
# Hand-rolled fakes
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool
        self._active_tx: "_FakeTransaction | None" = None
        self._pending_inserts: list[UUID] = []

    async def execute(self, sql: str, *args: Any) -> str:
        return self._pool._execute(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        row = self._pool._fetchrow(sql, args)
        if (
            row is not None
            and self._active_tx is not None
            and "insert into automation.llm_providers" in sql.lower()
        ):
            self._pending_inserts.append(row["id"])
        return row

    async def fetch(self, sql: str, *args: Any) -> Any:
        return self._pool._fetch(sql, args)

    def transaction(self) -> "_FakeTransaction":
        return _FakeTransaction(self)


class _FakeAcquire:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._pool)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeTransaction:
    """Tracks commit/rollback and exposes the call counters to tests.

    The fake threads itself through ``_FakeConn.transaction()`` so each
    ``_FakeConn`` instance shares the same tx object — that lets
    :meth:`_FakeConn.fetchrow` see whether the surrounding transaction
    rolled back and undo any speculative INSERT into the pool's
    in-memory dict.
    """

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True
        self._conn._active_tx = self

    async def commit(self) -> None:
        self.committed = True
        self._conn._pending_inserts.clear()
        self._conn._active_tx = None

    async def rollback(self) -> None:
        self.rolled_back = True
        # Undo any speculative INSERTs queued during the transaction.
        for provider_id in self._conn._pending_inserts:
            self._conn._pool.providers.pop(provider_id, None)
        self._conn._pending_inserts.clear()
        self._conn._active_tx = None


class _FakePool:
    """In-memory pool — stores rows in dicts indexed by provider_id."""

    def __init__(self) -> None:
        self.providers: dict[UUID, dict[str, Any]] = {}
        self.overrides: dict[str, UUID] = {}

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    def _fetchrow(self, sql: str, args: tuple) -> Any:
        sql_low = sql.lower()
        if "insert into automation.llm_providers" in sql_low:
            (
                provider_id,
                provider_type,
                name,
                model,
                context_length,
                base_url,
                vault_path,
                reasoning_effort,
                verbosity,
            ) = args
            now = datetime.now(timezone.utc)
            row = {
                "id": provider_id,
                "provider_type": provider_type,
                "name": name,
                "model": model,
                "context_length": context_length,
                "base_url": base_url,
                "vault_path": vault_path,
                "status": "active",
                "reasoning_effort": reasoning_effort,
                "verbosity": verbosity,
                "last_tested_at": None,
                "last_test_error": None,
                "created_at": now,
                "updated_at": now,
            }
            self.providers[provider_id] = row
            return row
        if "update automation.llm_providers" in sql_low and "set name" in sql_low:
            (
                provider_id,
                name,
                model,
                context_length,
                base_url,
                status,
                reasoning_effort,
                verbosity,
            ) = args
            row = self.providers.get(provider_id)
            if row is None:
                return None
            if name is not None:
                row["name"] = name
            if model is not None:
                row["model"] = model
            if context_length is not None:
                row["context_length"] = context_length
            if base_url is not None:
                row["base_url"] = base_url
            if status is not None:
                row["status"] = status
            if reasoning_effort is not None:
                row["reasoning_effort"] = reasoning_effort
            if verbosity is not None:
                row["verbosity"] = verbosity
            row["updated_at"] = datetime.now(timezone.utc)
            return row
        if "select" in sql_low and "from automation.llm_providers" in sql_low:
            (provider_id,) = args
            return self.providers.get(provider_id)
        return None

    def _fetch(self, sql: str, args: tuple) -> list[Any]:
        sql_low = sql.lower()
        if "from automation.llm_providers" in sql_low:
            return sorted(
                self.providers.values(),
                key=lambda r: r["created_at"],
                reverse=True,
            )
        if "from automation.dept_llm_provider_overrides" in sql_low:
            (provider_id,) = args
            return [
                {"dept_id": d}
                for d, p in sorted(self.overrides.items())
                if p == provider_id
            ]
        return []

    def _execute(self, sql: str, args: tuple) -> str:
        sql_low = sql.lower()
        if "delete from automation.llm_providers" in sql_low:
            (provider_id,) = args
            removed = self.providers.pop(provider_id, None)
            return f"DELETE {1 if removed else 0}"
        if "update automation.llm_providers" in sql_low and "last_tested_at" in sql_low:
            provider_id, last_tested_at, last_test_error = args
            row = self.providers.get(provider_id)
            if row is not None:
                row["last_tested_at"] = last_tested_at
                row["last_test_error"] = last_test_error
            return "UPDATE 1"
        return "OK"


class _FakeVault:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.storage: dict[UUID, dict[str, str]] = {}
        self.writes: list[tuple[UUID, dict[str, str]]] = []
        self.deletes: list[UUID] = []
        self._fail_on = fail_on or set()

    async def write_llm_credentials(
        self, *, provider_id: UUID, payload: dict[str, str]
    ) -> None:
        if "write" in self._fail_on:
            raise VaultWriteError(
                operation="write",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=500,
                message="forced failure",
            )
        self.storage[provider_id] = dict(payload)
        self.writes.append((provider_id, dict(payload)))

    async def read_llm_credentials(
        self, *, provider_id: UUID
    ) -> dict[str, str]:
        return dict(self.storage.get(provider_id, {}))

    async def delete_llm_credentials(
        self, *, provider_id: UUID
    ) -> None:
        if "delete" in self._fail_on:
            raise VaultWriteError(
                operation="delete",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=500,
                message="forced failure",
            )
        self.storage.pop(provider_id, None)
        self.deletes.append(provider_id)


class _RecordingAuditSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)


class _FakeTester:
    def __init__(self) -> None:
        self.calls: list[TestRequest] = []
        self.result = ConnectionTestResult(
            success=True, latency_ms=12, model="echo-model", error=None
        )

    async def run(self, req: TestRequest) -> ConnectionTestResult:
        self.calls.append(req)
        return self.result


def _make_repo_and_override_repo() -> tuple[Any, Any]:
    from src.llm_providers.repository import LLMProviderRepository
    from src.llm_providers.dept_override_repository import DeptOverrideRepository

    return LLMProviderRepository(), DeptOverrideRepository()


def _build_service(
    *,
    pool: _FakePool,
    vault: _FakeVault,
    tester: _FakeTester | None = None,
    audit: _RecordingAuditSink | None = None,
) -> tuple[ProviderService, _RecordingAuditSink, _FakeTester]:
    tester = tester or _FakeTester()
    audit = audit or _RecordingAuditSink()
    repo, override_repo = _make_repo_and_override_repo()
    service = ProviderService(
        pool=pool,
        vault_client=vault,
        repo=repo,
        override_repo=override_repo,
        connection_tester=tester,
        audit_sink=audit,
    )
    return service, audit, tester


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_mask_uses_last_four_chars() -> None:
    assert mask("sk-abc12345") == "…2345"
    assert mask("abc") == "…"
    assert mask(None) == "…"


@pytest.mark.asyncio
async def test_create_provider_writes_vault_and_returns_masked_dto() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, audit, _ = _build_service(pool=pool, vault=vault)

    payload = _ProviderCreateInput(
        provider_type="openai",
        name="My OpenAI",
        model="gpt-4o-mini",
        context_length=128000,
        base_url=None,
        api_key="sk-test-1234567890ABCDEFGHIJK",
        org_id="org-xyz",
    )

    dto = await service.create(payload, actor_id="admin-1")

    assert isinstance(dto, LLMProviderConfigDTO)
    assert dto.provider_type == "openai"
    assert dto.api_key_masked.endswith("HIJK")
    assert dto.api_key_masked.startswith("…")
    assert dto.org_id_masked is not None and dto.org_id_masked.endswith("-xyz")
    # Postgres + Vault both saw the write.
    assert len(pool.providers) == 1
    assert len(vault.writes) == 1
    assert "api_key" in vault.writes[0][1]
    # Audit emitted exactly once.
    assert len(audit.events) == 1
    assert audit.events[0].action == "llm_provider_created"


@pytest.mark.asyncio
async def test_create_vault_failure_rolls_back_postgres() -> None:
    pool = _FakePool()
    vault = _FakeVault(fail_on={"write"})
    service, _, _ = _build_service(pool=pool, vault=vault)

    payload = _ProviderCreateInput(
        provider_type="anthropic",
        name="Claude",
        model="claude-3-5-sonnet",
        context_length=200000,
        base_url=None,
        api_key="sk-ant-test1234567890ABCDEFGH",
        org_id=None,
    )

    with pytest.raises(VaultWriteFailed):
        await service.create(payload, actor_id="admin-1")
    # Row must not survive a Vault failure.
    assert pool.providers == {}


@pytest.mark.asyncio
async def test_update_without_api_key_preserves_credentials() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, _, _ = _build_service(pool=pool, vault=vault)

    payload = _ProviderCreateInput(
        provider_type="openai",
        name="My OpenAI",
        model="gpt-4o-mini",
        context_length=128000,
        base_url=None,
        api_key="sk-test-original-1234567890ABCDEFGH",
        org_id=None,
    )
    dto = await service.create(payload, actor_id="admin-1")
    original_writes = list(vault.writes)

    # Update WITHOUT api_key — Vault must not be touched.
    patch = ProviderUpdate(name="Renamed")
    updated = await service.update(dto.id, patch, actor_id="admin-1")
    assert updated is not None
    assert updated.name == "Renamed"
    assert vault.writes == original_writes  # no extra Vault write


@pytest.mark.asyncio
async def test_update_with_api_key_writes_vault() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, _, _ = _build_service(pool=pool, vault=vault)

    payload = _ProviderCreateInput(
        provider_type="openai",
        name="My OpenAI",
        model="gpt-4o-mini",
        context_length=128000,
        base_url=None,
        api_key="sk-original-12345678901234567890",
        org_id=None,
    )
    dto = await service.create(payload, actor_id="admin-1")

    patch = ProviderUpdate(api_key="sk-rotated-09876543210987654321")
    updated = await service.update(dto.id, patch, actor_id="admin-1")
    assert updated is not None
    assert vault.storage[dto.id]["api_key"] == (
        "sk-rotated-09876543210987654321"
    )


@pytest.mark.asyncio
async def test_delete_blocks_when_dept_pins_provider() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, _, _ = _build_service(pool=pool, vault=vault)

    payload = _ProviderCreateInput(
        provider_type="anthropic",
        name="Claude",
        model="claude-3-5-sonnet",
        context_length=200000,
        base_url=None,
        api_key="sk-ant-test1234567890ABCDEFGH",
        org_id=None,
    )
    dto = await service.create(payload, actor_id="admin-1")
    # Simulate a dept pinning this provider.
    pool.overrides["payment-ops"] = dto.id

    with pytest.raises(ProviderInUse) as exc_info:
        await service.delete(dto.id, actor_id="admin-1")
    assert exc_info.value.dept_ids == ["payment-ops"]
    # Row must remain — referential safety.
    assert dto.id in pool.providers
    assert vault.deletes == []


@pytest.mark.asyncio
async def test_delete_returns_false_when_provider_missing() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, _, _ = _build_service(pool=pool, vault=vault)

    deleted = await service.delete(uuid4(), actor_id="admin-1")
    assert deleted is False


@pytest.mark.asyncio
async def test_set_override_rejects_missing_provider() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, _, _ = _build_service(pool=pool, vault=vault)

    with pytest.raises(ProviderNotFound):
        await service.set_override(
            "payment-ops", uuid4(), actor_id="admin-1"
        )


@pytest.mark.asyncio
async def test_set_override_rejects_inactive_provider() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, _, _ = _build_service(pool=pool, vault=vault)

    payload = _ProviderCreateInput(
        provider_type="anthropic",
        name="Claude",
        model="claude-3-5-sonnet",
        context_length=200000,
        base_url=None,
        api_key="sk-ant-test1234567890ABCDEFGH",
        org_id=None,
    )
    dto = await service.create(payload, actor_id="admin-1")

    # Disable via update.
    await service.update(
        dto.id, ProviderUpdate(status="inactive"), actor_id="admin-1"
    )

    with pytest.raises(ProviderInactive):
        await service.set_override(
            "payment-ops", dto.id, actor_id="admin-1"
        )


@pytest.mark.asyncio
async def test_test_unsaved_runs_against_tester_without_db_touch() -> None:
    pool = _FakePool()
    vault = _FakeVault()
    service, _, tester = _build_service(pool=pool, vault=vault)

    payload = UnsavedTestRequest(
        provider_type="openai",
        name="probe",
        model="gpt-4o-mini",
        context_length=128000,
        api_key="sk-probe-1234567890ABCDEFGHIJ",
    )
    result = await service.test_unsaved(payload, actor_id="admin-1")
    assert result.success is True
    # Tester saw the request; DB / Vault untouched.
    assert len(tester.calls) == 1
    assert pool.providers == {}
    assert vault.writes == []
