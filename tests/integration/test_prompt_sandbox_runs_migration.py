"""Integration test 1.4 — ``001_prompt_sandbox_runs.sql`` migration.

Spec: ``.kiro/specs/platform-mimari-uyumluluk/tasks.md`` task 1.4.

Validates that the ``infra/postgres/migrations/001_prompt_sandbox_runs.sql``
migration applies cleanly on a fresh ``postgres:16-alpine`` container and
produces the table shape the design document declares:

1. Schema bootstrap (``00_schemas.sql``) followed by the migration must
   succeed without errors and be idempotent (re-running is a no-op).
2. ``automation.prompt_sandbox_runs`` exists with **all** the columns
   listed in design §"R7 — Prompt Promote Endpoint + ``prompt_sandbox_runs``"
   — covering Requirement 7.2.
3. The composite index ``idx_prompt_sandbox_runs_path_created`` exists on
   ``(prompt_path, created_at DESC)``.

Gating
------
The test is gated behind the workspace-level ``--run-docker`` flag
(registered in ``tests/conftest.py``). Without the flag the test skips
cleanly so the default property/unit lane stays self-contained.
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


# Expected column → Postgres ``data_type`` (information_schema) mapping.
# Values mirror the migration's CREATE TABLE statement.
EXPECTED_COLUMNS: dict[str, str] = {
    "id": "uuid",
    "prompt_path": "text",
    "draft_branch": "text",
    "sample_input": "text",
    "prompt_body_hash": "text",
    "response_text": "text",
    "token_in": "integer",
    "token_out": "integer",
    "cost_usd": "numeric",
    "passed": "boolean",
    "created_at": "timestamp with time zone",
    "actor_id": "text",
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
            "-At",  # unaligned, tuples-only
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
    """Start a fresh Postgres container for the test module."""

    if not request.config.getoption("--run-docker"):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker to enable."
        )

    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable; cannot run prompt_sandbox_runs migration test."
        )

    container_name = f"test-prompt-sandbox-runs-{uuid.uuid4().hex[:8]}"

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
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
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
def sql_paths(repo_root: Path) -> tuple[Path, Path]:
    schemas_sql = repo_root / "infra" / "postgres" / "00_schemas.sql"
    migration_sql = (
        repo_root / "infra" / "postgres" / "migrations"
        / "001_prompt_sandbox_runs.sql"
    )
    assert schemas_sql.is_file(), f"Missing: {schemas_sql}"
    assert migration_sql.is_file(), f"Missing: {migration_sql}"
    return schemas_sql, migration_sql


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPromptSandboxRunsMigration:
    """Validates Requirement 7.2 — prompt_sandbox_runs schema."""

    def test_migration_applies_cleanly(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """``00_schemas.sql`` + the migration must apply without psql errors."""

        schemas_sql, migration_sql = sql_paths

        r1 = _run_sql_file(pg_container, schemas_sql)
        assert r1.returncode == 0, (
            f"00_schemas.sql failed:\n  stdout: {r1.stdout}\n  stderr: {r1.stderr}"
        )

        r2 = _run_sql_file(pg_container, migration_sql)
        assert r2.returncode == 0, (
            "001_prompt_sandbox_runs.sql failed:\n"
            f"  stdout: {r2.stdout}\n  stderr: {r2.stderr}"
        )

    def test_migration_is_idempotent(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """Re-running the migration on an already-migrated DB is a no-op."""

        schemas_sql, migration_sql = sql_paths

        # First apply (may already be done by the previous test on the
        # module-scoped container; either way subsequent runs must succeed).
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, migration_sql)

        # Idempotent re-run.
        r = _run_sql_file(pg_container, migration_sql)
        assert r.returncode == 0, (
            "Re-running 001_prompt_sandbox_runs.sql produced an error "
            f"(expected idempotent IF NOT EXISTS):\n"
            f"  stdout: {r.stdout}\n  stderr: {r.stderr}"
        )

    def test_table_exists_in_automation_schema(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """``automation.prompt_sandbox_runs`` table must exist."""

        schemas_sql, migration_sql = sql_paths
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, migration_sql)

        result = _run_sql(
            pg_container,
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'automation'
              AND table_name = 'prompt_sandbox_runs';
            """,
        )
        assert result.returncode == 0, f"Table query failed: {result.stderr}"
        tables = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert tables == ["prompt_sandbox_runs"], (
            "automation.prompt_sandbox_runs not found after migration; "
            f"got tables={tables!r}"
        )

    def test_table_columns_match_design(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """All columns from design §R7 must be present with the expected types."""

        schemas_sql, migration_sql = sql_paths
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, migration_sql)

        result = _run_sql(
            pg_container,
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'automation'
              AND table_name = 'prompt_sandbox_runs'
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

        # Every expected column must be present with the expected type.
        for col, expected_type in EXPECTED_COLUMNS.items():
            assert col in actual, (
                f"Column {col!r} missing from automation.prompt_sandbox_runs; "
                f"got columns={sorted(actual)!r}"
            )
            assert actual[col] == expected_type, (
                f"Column {col!r} has unexpected data_type "
                f"(expected {expected_type!r}, got {actual[col]!r})"
            )

        # No unexpected extras — keep the schema tight.
        unexpected = set(actual) - set(EXPECTED_COLUMNS)
        assert not unexpected, (
            f"Unexpected columns in automation.prompt_sandbox_runs: {sorted(unexpected)!r}"
        )

    def test_id_is_primary_key_with_uuid_default(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """``id`` must be the PK with ``gen_random_uuid()`` default."""

        schemas_sql, migration_sql = sql_paths
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, migration_sql)

        # PK check
        pk = _run_sql(
            pg_container,
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = 'automation'
              AND tc.table_name = 'prompt_sandbox_runs'
              AND tc.constraint_type = 'PRIMARY KEY';
            """,
        )
        assert pk.returncode == 0
        pk_cols = [line.strip() for line in pk.stdout.splitlines() if line.strip()]
        assert pk_cols == ["id"], (
            f"Expected PK on (id,), got {pk_cols!r}"
        )

        # Default check (must reference gen_random_uuid()).
        default = _run_sql(
            pg_container,
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = 'automation'
              AND table_name = 'prompt_sandbox_runs'
              AND column_name = 'id';
            """,
        )
        assert default.returncode == 0
        assert "gen_random_uuid" in default.stdout, (
            f"id default does not reference gen_random_uuid(); got {default.stdout!r}"
        )

    def test_passed_column_has_default_false(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """``passed`` column must default to FALSE per the migration."""

        schemas_sql, migration_sql = sql_paths
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, migration_sql)

        result = _run_sql(
            pg_container,
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = 'automation'
              AND table_name = 'prompt_sandbox_runs'
              AND column_name = 'passed';
            """,
        )
        assert result.returncode == 0
        # Postgres normalizes the default to ``false`` for BOOLEAN.
        assert "false" in result.stdout.lower(), (
            f"passed.default expected FALSE; got {result.stdout!r}"
        )

    def test_index_exists_on_path_and_created(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """``idx_prompt_sandbox_runs_path_created`` must exist."""

        schemas_sql, migration_sql = sql_paths
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, migration_sql)

        result = _run_sql(
            pg_container,
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'automation'
              AND tablename = 'prompt_sandbox_runs'
            ORDER BY indexname;
            """,
        )
        assert result.returncode == 0, f"Index query failed: {result.stderr}"

        rows: dict[str, str] = {}
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name, _, definition = stripped.partition("|")
            rows[name.strip()] = definition.strip()

        assert "idx_prompt_sandbox_runs_path_created" in rows, (
            "Expected index idx_prompt_sandbox_runs_path_created not found; "
            f"got {sorted(rows)!r}"
        )

        index_def = rows["idx_prompt_sandbox_runs_path_created"]
        # Sanity-check the columns and ordering captured in the index def.
        assert "prompt_path" in index_def, (
            f"Index def missing prompt_path: {index_def!r}"
        )
        assert "created_at" in index_def, (
            f"Index def missing created_at: {index_def!r}"
        )
        # ``DESC`` is recorded in pg_indexes definitions when explicitly set.
        assert "DESC" in index_def.upper(), (
            f"Index def missing DESC ordering: {index_def!r}"
        )

    def test_insert_round_trip_with_defaults(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """A minimal INSERT must populate id (uuid), created_at, and passed."""

        schemas_sql, migration_sql = sql_paths
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, migration_sql)

        # Clean any prior state from the module-scoped container.
        _run_sql(pg_container, "DELETE FROM automation.prompt_sandbox_runs;")

        ins = _run_sql(
            pg_container,
            """
            INSERT INTO automation.prompt_sandbox_runs
                (prompt_path, draft_branch)
            VALUES ('prompts/example.md', 'feature/draft-1');
            """,
        )
        assert ins.returncode == 0, f"Minimal insert failed: {ins.stderr}"

        sel = _run_sql(
            pg_container,
            """
            SELECT id::text, prompt_path, draft_branch,
                   passed, created_at IS NOT NULL
            FROM automation.prompt_sandbox_runs;
            """,
        )
        assert sel.returncode == 0
        rows = [line for line in sel.stdout.splitlines() if line.strip()]
        assert len(rows) == 1, f"Expected exactly one row; got {rows!r}"
        id_text, prompt_path, draft_branch, passed, created_not_null = (
            rows[0].split("|")
        )
        # Default UUID populated.
        uuid.UUID(id_text)
        assert prompt_path == "prompts/example.md"
        assert draft_branch == "feature/draft-1"
        # ``passed`` defaults to FALSE → psql renders as 'f'.
        assert passed.lower() in ("f", "false")
        assert created_not_null.lower() in ("t", "true")
