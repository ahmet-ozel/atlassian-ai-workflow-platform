"""Hand-rolled fakes shared by the LLM provider property suite.

The fakes cover the four collaborators every ``ProviderService``
exercise needs:

* :class:`FakePool` — in-memory asyncpg pool with a transaction
  rollback model that undoes speculative INSERTs.
* :class:`FakeVault` — KV-v2 store with toggles for write / delete
  failures.
* :class:`FakeTester` — :class:`ConnectionTester` stand-in that
  records every :class:`TestRequest` it receives.
* :class:`RecordingAuditSink` — captures every emitted audit event
  for the property assertions.

The helpers exist as a module so the various property tests share
exactly the same fake shape; importing fixes the sys.path bootstrap
once and exposes the constructors via a single namespace.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


from src.lifecycle.vault_client import VaultWriteError  # noqa: E402
from src.llm_providers.connection_tester import TestRequest  # noqa: E402
from src.llm_providers.dept_override_repository import (  # noqa: E402
    DeptOverrideRepository,
)
from src.llm_providers.repository import LLMProviderRepository  # noqa: E402
from src.llm_providers.schemas import (  # noqa: E402
    ConnectionTestResult,
)
from src.llm_providers.service import ProviderService  # noqa: E402


# ---------------------------------------------------------------------------
# Pool fakes — transaction-aware so rollback semantics are observable.
# ---------------------------------------------------------------------------


@dataclass
class _FakeTransactionState:
    pending_inserts: list[UUID] = field(default_factory=list)


class _FakeTransaction:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True
        self._conn._active_tx = self

    async def commit(self) -> None:
        self.committed = True
        self._conn._state.pending_inserts.clear()
        self._conn._active_tx = None

    async def rollback(self) -> None:
        self.rolled_back = True
        for pid in self._conn._state.pending_inserts:
            self._conn._pool.providers.pop(pid, None)
        self._conn._state.pending_inserts.clear()
        self._conn._active_tx = None


class FakeConn:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool
        self._active_tx: _FakeTransaction | None = None
        self._state = _FakeTransactionState()

    async def execute(self, sql: str, *args: Any) -> str:
        return self._pool._execute(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        row = self._pool._fetchrow(sql, args)
        if (
            row is not None
            and self._active_tx is not None
            and "insert into automation.llm_providers" in sql.lower()
        ):
            self._state.pending_inserts.append(row["id"])
        return row

    async def fetch(self, sql: str, *args: Any) -> Any:
        return self._pool._fetch(sql, args)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)


class _FakeAcquire:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool

    async def __aenter__(self) -> FakeConn:
        return FakeConn(self._pool)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakePool:
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
        if (
            "update automation.llm_providers" in sql_low
            and "set name" in sql_low
        ):
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
        if (
            "insert into automation.dept_llm_provider_overrides"
            in sql_low
        ):
            dept_id, provider_id = args
            self.overrides[dept_id] = provider_id
            return {
                "dept_id": dept_id,
                "provider_id": provider_id,
                "created_at": datetime.now(timezone.utc),
            }
        if (
            "select" in sql_low
            and "from automation.llm_providers" in sql_low
        ):
            (provider_id,) = args
            return self.providers.get(provider_id)
        if (
            "select" in sql_low
            and "from automation.dept_llm_provider_overrides" in sql_low
        ):
            (dept_id,) = args
            pid = self.overrides.get(dept_id)
            if pid is None:
                return None
            return {
                "dept_id": dept_id,
                "provider_id": pid,
                "created_at": datetime.now(timezone.utc),
            }
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
        if "delete from automation.dept_llm_provider_overrides" in sql_low:
            (dept_id,) = args
            self.overrides.pop(dept_id, None)
            return "DELETE 1"
        if (
            "update automation.llm_providers" in sql_low
            and "last_tested_at" in sql_low
        ):
            provider_id, last_tested_at, last_test_error = args
            row = self.providers.get(provider_id)
            if row is not None:
                row["last_tested_at"] = last_tested_at
                row["last_test_error"] = last_test_error
            return "UPDATE 1"
        return "OK"


# ---------------------------------------------------------------------------
# Vault / Tester / Audit fakes
# ---------------------------------------------------------------------------


class FakeVault:
    """In-memory KV-v2 stand-in with failure toggles for property tests."""

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        rollback_raises: bool = False,
    ) -> None:
        self.storage: dict[UUID, dict[str, str]] = {}
        self.writes: list[tuple[UUID, dict[str, str]]] = []
        self.deletes: list[UUID] = []
        self._fail_on = set(fail_on or ())
        self._rollback_raises = rollback_raises

    @property
    def rollback_raises(self) -> bool:
        return self._rollback_raises

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


class FakeTester:
    """:class:`ConnectionTester` stand-in returning a fixed result."""

    def __init__(
        self,
        result: ConnectionTestResult | None = None,
    ) -> None:
        self.calls: list[TestRequest] = []
        self.result = result or ConnectionTestResult(
            success=True, latency_ms=10, model="echo", error=None
        )

    async def run(self, req: TestRequest) -> ConnectionTestResult:
        self.calls.append(req)
        return self.result


class RecordingAuditSink:
    """Captures every emitted :class:`AuditEvent` for assertions."""

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.events: list[Any] = []
        self._raise_on = set(raise_on or ())

    async def write(self, event: Any) -> None:
        if event.action in self._raise_on:
            raise RuntimeError(
                f"forced audit-sink failure for action={event.action!r}"
            )
        self.events.append(event)


# ---------------------------------------------------------------------------
# Service builder
# ---------------------------------------------------------------------------


def build_service(
    *,
    pool: FakePool | None = None,
    vault: FakeVault | None = None,
    tester: FakeTester | None = None,
    audit: RecordingAuditSink | None = None,
) -> tuple[ProviderService, FakePool, FakeVault, FakeTester, RecordingAuditSink]:
    """Build a :class:`ProviderService` wired entirely to in-memory fakes."""

    pool = pool or FakePool()
    vault = vault or FakeVault()
    tester = tester or FakeTester()
    audit = audit or RecordingAuditSink()

    if vault.rollback_raises:
        original_rollback = _FakeTransaction.rollback

        async def _rollback_with_raise(self: _FakeTransaction) -> None:
            await original_rollback(self)
            raise RuntimeError("forced rollback failure")

        _FakeTransaction.rollback = _rollback_with_raise  # type: ignore[assignment]

    service = ProviderService(
        pool=pool,
        vault_client=vault,
        repo=LLMProviderRepository(),
        override_repo=DeptOverrideRepository(),
        connection_tester=tester,
        audit_sink=audit,
    )
    return service, pool, vault, tester, audit


def random_provider_id() -> UUID:
    return uuid4()
