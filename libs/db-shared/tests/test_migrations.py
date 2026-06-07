"""Unit tests for ``db_shared.migrations.apply_migrations``.

The tests use an in-memory fake pool/connection so they run without
Postgres. They cover the idempotency contract, file discovery,
checksum-mismatch handling, and failure surface.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from db_shared.migrations import (
    SCHEMA_MIGRATIONS_DDL,
    MigrationError,
    apply_migrations,
    discover_migrations,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeTransaction:
    """asyncpg-shaped async context manager - entered/exited; no-op body."""

    async def __aenter__(self) -> "_FakeTransaction":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool | None:
        return None


class _FakeConn:
    """Records every ``execute`` / ``fetch`` call.

    ``execute`` simulates SQL - INSERT INTO schema_migrations updates an
    in-memory dict; everything else is recorded verbatim. ``fetch`` only
    serves the schema_migrations SELECT path.
    """

    def __init__(self, raise_on: str | None = None) -> None:
        self.applied: dict[str, str] = {}
        self.executed: list[str] = []
        self._raise_on = raise_on

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> None:
        if self._raise_on and self._raise_on in sql:
            raise RuntimeError(f"simulated SQL failure for: {self._raise_on}")
        self.executed.append(sql)
        if "INSERT INTO shared.schema_migrations" in sql:
            version, checksum = args
            self.applied[version] = checksum

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        if "SELECT version, checksum FROM shared.schema_migrations" in sql:
            return [
                {"version": v, "checksum": c} for v, c in self.applied.items()
            ]
        return []


class _FakePool:
    """Pool whose ``acquire()`` always yields the same fake connection."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _ctx() -> Any:
            yield self._conn

        return _ctx()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "001_first.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.t_first (id int);"
    )
    (d / "002_second.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.t_second (id int);"
    )
    (d / "003_third.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.t_third (id int);"
    )
    # In-progress draft - should be skipped by discovery.
    (d / "_draft.sql").write_text("/* not yet ready */")
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_discover_skips_underscore_and_dot(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    (d / "001_a.sql").write_text("--")
    (d / "_draft.sql").write_text("--")
    (d / ".hidden.sql").write_text("--")
    found = discover_migrations(d)
    assert [p.name for p in found] == ["001_a.sql"]


def test_discover_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert discover_migrations(tmp_path / "does-not-exist") == []


@pytest.mark.asyncio
async def test_apply_all_on_fresh_db(migrations_dir: Path) -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)

    result = await apply_migrations(pool, migrations_dir)

    assert [m.version for m in result.newly_applied] == [
        "001_first",
        "002_second",
        "003_third",
    ]
    assert result.already_applied == []
    assert result.checksum_mismatches == []
    # schema_migrations DDL must have been issued.
    assert any("CREATE TABLE IF NOT EXISTS shared.schema_migrations" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_rerun_is_noop(migrations_dir: Path) -> None:
    """Idempotency contract: a second invocation applies nothing."""
    conn = _FakeConn()
    pool = _FakePool(conn)

    await apply_migrations(pool, migrations_dir)
    second = await apply_migrations(pool, migrations_dir)

    assert second.newly_applied == []
    assert sorted(second.already_applied) == [
        "001_first",
        "002_second",
        "003_third",
    ]


@pytest.mark.asyncio
async def test_checksum_mismatch_logged_not_reapplied(
    migrations_dir: Path,
) -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)

    # First pass - record the original checksums.
    await apply_migrations(pool, migrations_dir)
    original_executions = len(conn.executed)

    # Tamper with one migration file on disk.
    (migrations_dir / "002_second.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.t_second_TAMPERED (id int);"
    )

    second = await apply_migrations(pool, migrations_dir)

    assert second.newly_applied == []
    mismatched_versions = [m.version for m in second.checksum_mismatches]
    assert mismatched_versions == ["002_second"]
    # And critically: no extra SQL was executed for the mismatched file.
    # The DDL + SELECT happen on every call, so allow at most 2 new ops.
    new_executions = len(conn.executed) - original_executions
    assert new_executions <= 2, (
        f"runner re-applied a mismatched migration; executed {new_executions} "
        "extra statements"
    )


@pytest.mark.asyncio
async def test_failure_raises_migration_error(migrations_dir: Path) -> None:
    """A failing SQL statement surfaces as :class:`MigrationError`."""
    conn = _FakeConn(raise_on="t_second")
    pool = _FakePool(conn)

    with pytest.raises(MigrationError) as exc:
        await apply_migrations(pool, migrations_dir)

    assert exc.value.version == "002_second"


def test_schema_migrations_ddl_is_idempotent() -> None:
    """The DDL string MUST contain ``IF NOT EXISTS`` so it can run on
    every boot without crashing."""
    assert "CREATE SCHEMA IF NOT EXISTS shared" in SCHEMA_MIGRATIONS_DDL
    assert "CREATE TABLE IF NOT EXISTS shared.schema_migrations" in SCHEMA_MIGRATIONS_DDL


def test_real_migration_dir_has_expected_files() -> None:
    """Smoke check: the unified ``infra/postgres/migrations/`` tree has
    the migrations we expect after the K1/Y5 consolidation (no gaps in
    sequence 001..018)."""
    # Resolve repo root: this file lives at
    # platform/libs/db-shared/tests/test_migrations.py
    repo_root = Path(__file__).resolve().parents[3]
    migrations = repo_root / "infra" / "postgres" / "migrations"
    if not migrations.is_dir():
        pytest.skip("real migration dir not available in this test layout")

    files = discover_migrations(migrations)
    prefixes = sorted({p.name.split("_", 1)[0] for p in files})
    # 001..018 contiguous after consolidation; any gap indicates a lost
    # migration file.
    expected = [f"{i:03d}" for i in range(1, 19)]
    missing = [p for p in expected if p not in prefixes]
    assert not missing, f"missing migration prefixes: {missing}"

    # The 013_ssh_runner_pool / 010_llm_providers / 011_test_runs MUST
    # be present - these are the tables admin-dashboard-api needs.
    names = {p.stem for p in files}
    assert "018_ssh_runner_pool" in names
    assert "010_llm_providers" in names
    assert "011_test_runs" in names
