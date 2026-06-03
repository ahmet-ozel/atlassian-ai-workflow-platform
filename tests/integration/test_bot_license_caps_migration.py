"""Integration test 1.4 — ``002_bot_license_caps.sql`` migration.

.

Validates that the ``infra/postgres/migrations/002_bot_license_caps.sql``
migration applies cleanly on top of the workspace schema bootstrap
(``00_schemas.sql`` + ``10_automation.sql``) and produces:

1. ``automation.bot_license_caps`` table with the design-mandated columns
 and defaults (``max_concurrent_workflows=10``,
 ``max_workflows_per_day=100``, ``max_token_usd_per_month=1000.00``).
2. ``automation.departments.license_id`` nullable FK column referencing
 ``bot_license_caps(license_id)``.
3. The FK constraint is enforced — inserting a department row with a
 ``license_id`` that does not exist in ``bot_license_caps`` is rejected.
4. The migration is idempotent (re-running is a no-op).

Gating
------
Behind the workspace-level ``--run-docker`` flag (registered in
``tests/conftest.py``).
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PG_IMAGE: str = "postgres:16-alpine"
PG_USER: str = "test_user"
PG_PASSWORD: str = "test_pass"
PG_DB: str = "test_db"

PG_READY_TIMEOUT: float = 30.0
POLL_INTERVAL: float = 0.5


EXPECTED_BOT_LICENSE_CAPS_COLUMNS: dict[str, str] = {
    "id": "uuid",
    "license_id": "text",
    "max_concurrent_workflows": "integer",
    "max_workflows_per_day": "integer",
    "max_token_usd_per_month": "numeric",
    "created_at": "timestamp with time zone",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _wait_for_pg(container_name: str, timeout: float) -> bool:
    """Wait until Postgres accepts a real ``SELECT 1`` against ``PG_DB``.

 ``pg_isready`` alone is not sufficient: the official ``postgres:16-alpine``
 image starts the server briefly during entrypoint bootstrap before the
 init scripts have created the user database, so ``pg_isready`` can
 return success momentarily while ``psql -d test_db`` still gets
 ``database "test_db" does not exist``. Probing with a real SELECT
 closes that race.
 """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = subprocess.run(
            [
                "docker", "exec", container_name,
                "pg_isready", "-U", PG_USER, "-d", PG_DB,
            ],
            capture_output=True,
            check=False,
        )
        if ready.returncode == 0:
            verify = subprocess.run(
                [
                    "docker", "exec", "-i", container_name,
                    "psql", "-U", PG_USER, "-d", PG_DB,
                    "-At",
                    "-v", "ON_ERROR_STOP=1",
                    "-c", "SELECT 1",
                ],
                capture_output=True,
                check=False,
            )
            if verify.returncode == 0:
                return True
        time.sleep(POLL_INTERVAL)
    return False


def _run_sql_file(container_name: str, sql_path: Path) -> subprocess.CompletedProcess:
    """Run a SQL file via psql, forcing UTF-8 on stdin/stdout/stderr.

 The SQL files contain non-ASCII characters in comments (em-dashes,
 arrows, Turkish letters); on Windows the default subprocess codec
 (cp1254) cannot encode those, so we explicitly request UTF-8.
 """

    sql_content = sql_path.read_text(encoding="utf-8")
    return subprocess.run(
        [
            "docker", "exec", "-i", container_name,
            "psql", "-U", PG_USER, "-d", PG_DB,
            "-v", "ON_ERROR_STOP=1",
        ],
        input=sql_content,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_sql(container_name: str, sql: str) -> subprocess.CompletedProcess:
    """Run an inline SQL statement; force UTF-8 stdin/stdout decoding."""

    return subprocess.run(
        [
            "docker", "exec", "-i", container_name,
            "psql", "-U", PG_USER, "-d", PG_DB,
            "-At",
            "-v", "ON_ERROR_STOP=1",
        ],
        input=sql,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container(request: pytest.FixtureRequest):
    if not request.config.getoption("--run-docker"):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker to enable."
        )

    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable; cannot run bot_license_caps migration test."
        )

    container_name = f"test-bot-license-caps-{uuid.uuid4().hex[:8]}"

    result = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", container_name,
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            PG_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"Failed to start Postgres container:\n"
        f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )

    assert _wait_for_pg(container_name, PG_READY_TIMEOUT), (
        f"Postgres container {container_name} did not become ready "
        f"within {PG_READY_TIMEOUT}s"
    )

    yield container_name

    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        check=False,
    )


@pytest.fixture()
def sql_paths(repo_root: Path) -> tuple[Path, Path, Path]:
    schemas_sql = repo_root / "infra" / "postgres" / "00_schemas.sql"
    automation_sql = repo_root / "infra" / "postgres" / "10_automation.sql"
    migration_sql = (
        repo_root / "infra" / "postgres" / "migrations"
        / "002_bot_license_caps.sql"
    )
    assert schemas_sql.is_file(), f"Missing: {schemas_sql}"
    assert automation_sql.is_file(), f"Missing: {automation_sql}"
    assert migration_sql.is_file(), f"Missing: {migration_sql}"
    return schemas_sql, automation_sql, migration_sql


def _apply_full_stack(container: str, paths: tuple[Path, Path, Path]) -> None:
    schemas_sql, automation_sql, migration_sql = paths
    for path in (schemas_sql, automation_sql, migration_sql):
        result = _run_sql_file(container, path)
        assert result.returncode == 0, (
            f"Applying {path.name} failed:\n"
            f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBotLicenseCapsMigration:
    """Validates bot_license_caps + FK column."""

    def test_migration_applies_cleanly(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """Schema bootstrap + automation + migration must all succeed."""

        _apply_full_stack(pg_container, sql_paths)

    def test_migration_is_idempotent(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """Re-running the migration on an already-migrated DB is a no-op."""

        _apply_full_stack(pg_container, sql_paths)

        # Re-run the migration only.
        _, _, migration_sql = sql_paths
        r = _run_sql_file(pg_container, migration_sql)
        assert r.returncode == 0, (
            "Re-running 002_bot_license_caps.sql produced an error "
            f"(expected idempotent IF NOT EXISTS):\n"
            f"  stdout: {r.stdout}\n  stderr: {r.stderr}"
        )

    def test_bot_license_caps_columns(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """All design-mandated columns must exist on bot_license_caps."""

        _apply_full_stack(pg_container, sql_paths)

        result = _run_sql(
            pg_container,
            """
 SELECT column_name, data_type
 FROM information_schema.columns
 WHERE table_schema = 'automation'
 AND table_name = 'bot_license_caps'
 ORDER BY column_name;
 """,
        )
        assert result.returncode == 0, f"Column query failed: {result.stderr}"

        actual: dict[str, str] = {}
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            col, _, dtype = stripped.partition("|")
            actual[col.strip()] = dtype.strip()

        for col, expected_type in EXPECTED_BOT_LICENSE_CAPS_COLUMNS.items():
            assert col in actual, (
                f"Column {col!r} missing from automation.bot_license_caps; "
                f"got columns={sorted(actual)!r}"
            )
            assert actual[col] == expected_type, (
                f"Column {col!r} has unexpected data_type "
                f"(expected {expected_type!r}, got {actual[col]!r})"
            )

        unexpected = set(actual) - set(EXPECTED_BOT_LICENSE_CAPS_COLUMNS)
        assert not unexpected, (
            f"Unexpected columns in automation.bot_license_caps: "
            f"{sorted(unexpected)!r}"
        )

    def test_bot_license_caps_defaults(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """Inserting a row with only license_id must populate the design defaults."""

        _apply_full_stack(pg_container, sql_paths)

        # Use a fresh license_id so the test is order-independent within
        # the module-scoped container.
        license_id = f"lic-defaults-{uuid.uuid4().hex[:8]}"
        ins = _run_sql(
            pg_container,
            f"""
            INSERT INTO automation.bot_license_caps (license_id)
            VALUES ('{license_id}');
            """,
        )
        assert ins.returncode == 0, f"Insert failed: {ins.stderr}"

        sel = _run_sql(
            pg_container,
            f"""
            SELECT max_concurrent_workflows,
                   max_workflows_per_day,
                   max_token_usd_per_month
            FROM automation.bot_license_caps
            WHERE license_id = '{license_id}';
            """,
        )
        assert sel.returncode == 0, f"Select failed: {sel.stderr}"

        row = next(
            (line for line in sel.stdout.splitlines() if line.strip()),
            None,
        )
        assert row is not None, (
            f"Inserted row not found for license_id={license_id!r}"
        )
        max_concurrent, max_per_day, max_token_usd = row.split("|")
        assert max_concurrent == "10", (
            f"max_concurrent_workflows default expected 10; got {max_concurrent!r}"
        )
        assert max_per_day == "100", (
            f"max_workflows_per_day default expected 100; got {max_per_day!r}"
        )
        # NUMERIC(10,2) renders as ``1000.00``.
        assert max_token_usd.startswith("1000"), (
            f"max_token_usd_per_month default expected ~1000.00; "
            f"got {max_token_usd!r}"
        )

    def test_license_id_is_unique(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """``license_id`` carries a UNIQUE constraint."""

        _apply_full_stack(pg_container, sql_paths)

        license_id = f"lic-uniq-{uuid.uuid4().hex[:8]}"
        first = _run_sql(
            pg_container,
            f"""
            INSERT INTO automation.bot_license_caps (license_id)
            VALUES ('{license_id}');
            """,
        )
        assert first.returncode == 0, f"First insert failed: {first.stderr}"

        dup = _run_sql(
            pg_container,
            f"""
            INSERT INTO automation.bot_license_caps (license_id)
            VALUES ('{license_id}');
            """,
        )
        assert dup.returncode != 0, (
            "Expected UNIQUE constraint violation for duplicate license_id"
        )
        combined = (dup.stderr + dup.stdout).lower()
        assert "duplicate key" in combined or "unique" in combined, (
            f"Expected unique-violation error; got {dup.stderr!r}"
        )

    def test_departments_license_id_column_exists(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """``automation.departments.license_id`` (nullable TEXT) must exist."""

        _apply_full_stack(pg_container, sql_paths)

        result = _run_sql(
            pg_container,
            """
 SELECT data_type, is_nullable
 FROM information_schema.columns
 WHERE table_schema = 'automation'
 AND table_name = 'departments'
 AND column_name = 'license_id';
 """,
        )
        assert result.returncode == 0
        row = next(
            (line for line in result.stdout.splitlines() if line.strip()),
            None,
        )
        assert row is not None, (
            "automation.departments.license_id column not found after migration"
        )
        data_type, is_nullable = row.split("|")
        assert data_type == "text", (
            f"departments.license_id expected text; got {data_type!r}"
        )
        assert is_nullable.upper() == "YES", (
            "departments.license_id must be NULLABLE the migration "
            f"; got is_nullable={is_nullable!r}"
        )

    def test_fk_targets_bot_license_caps_license_id(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """The FK on departments.license_id must reference bot_license_caps."""

        _apply_full_stack(pg_container, sql_paths)

        result = _run_sql(
            pg_container,
            """
 SELECT kcu.column_name,
 ccu.table_name,
 ccu.column_name
 FROM information_schema.table_constraints tc
 JOIN information_schema.key_column_usage kcu
 ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
 JOIN information_schema.constraint_column_usage ccu
 ON tc.constraint_name = ccu.constraint_name
 AND tc.table_schema = ccu.table_schema
 WHERE tc.table_schema = 'automation'
 AND tc.table_name = 'departments'
 AND tc.constraint_type = 'FOREIGN KEY'
 AND kcu.column_name = 'license_id';
 """,
        )
        assert result.returncode == 0, f"FK introspection failed: {result.stderr}"

        rows = [line for line in result.stdout.splitlines() if line.strip()]
        assert rows, (
            "No FOREIGN KEY constraint found on automation.departments.license_id; "
            "migration 002_bot_license_caps.sql did not register the reference."
        )

        # Every row should describe the same FK; pick the first.
        local_col, ref_table, ref_col = rows[0].split("|")
        assert local_col == "license_id"
        assert ref_table == "bot_license_caps", (
            f"departments.license_id FK expected to target bot_license_caps; "
            f"got {ref_table!r}"
        )
        assert ref_col == "license_id", (
            f"departments.license_id FK expected to target column license_id; "
            f"got {ref_col!r}"
        )

    def test_fk_rejects_unknown_license_id(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """Inserting a department with an unknown license_id is rejected."""

        _apply_full_stack(pg_container, sql_paths)

        # Make sure no orphan license slips in by accident; we use a
        # random id that is guaranteed not to exist.
        bogus = f"lic-missing-{uuid.uuid4().hex[:8]}"
        result = _run_sql(
            pg_container,
            f"""
            INSERT INTO automation.departments
                (id, display_name, license_id)
            VALUES ('dept-fk-test', 'FK Test', '{bogus}');
            """,
        )
        assert result.returncode != 0, (
            "Expected FK violation when inserting department with unknown "
            "license_id, but the insert succeeded."
        )
        combined = (result.stderr + result.stdout).lower()
        assert "foreign key" in combined or "violates" in combined, (
            f"Expected FK violation error; got {result.stderr!r}"
        )

    def test_fk_accepts_known_license_id(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """Inserting a department with a registered license_id succeeds."""

        _apply_full_stack(pg_container, sql_paths)

        license_id = f"lic-ok-{uuid.uuid4().hex[:8]}"
        dept_id = f"dept-ok-{uuid.uuid4().hex[:8]}"

        cap = _run_sql(
            pg_container,
            f"""
            INSERT INTO automation.bot_license_caps (license_id)
            VALUES ('{license_id}');
            """,
        )
        assert cap.returncode == 0, f"Cap insert failed: {cap.stderr}"

        dept = _run_sql(
            pg_container,
            f"""
            INSERT INTO automation.departments
                (id, display_name, license_id)
            VALUES ('{dept_id}', 'OK Test', '{license_id}');
            """,
        )
        assert dept.returncode == 0, (
            f"Department insert with valid license_id failed: {dept.stderr}"
        )

        # Round-trip read.
        sel = _run_sql(
            pg_container,
            f"""
            SELECT license_id
            FROM automation.departments
            WHERE id = '{dept_id}';
            """,
        )
        assert sel.returncode == 0
        assert sel.stdout.strip() == license_id, (
            f"Round-trip mismatch; expected {license_id!r}, got {sel.stdout!r}"
        )

    def test_fk_allows_null_license_id(
        self, pg_container: str, sql_paths: tuple[Path, Path, Path]
    ) -> None:
        """The FK column is nullable — opting out is allowed."""

        _apply_full_stack(pg_container, sql_paths)

        dept_id = f"dept-null-{uuid.uuid4().hex[:8]}"
        result = _run_sql(
            pg_container,
            f"""
            INSERT INTO automation.departments (id, display_name)
            VALUES ('{dept_id}', 'No License');
            """,
        )
        assert result.returncode == 0, (
            f"Department insert without license_id failed: {result.stderr}"
        )

        sel = _run_sql(
            pg_container,
            f"""
            SELECT license_id IS NULL
            FROM automation.departments
            WHERE id = '{dept_id}';
            """,
        )
        assert sel.returncode == 0
        assert sel.stdout.strip().lower() in ("t", "true"), (
            f"departments.license_id expected NULL; got {sel.stdout!r}"
        )
