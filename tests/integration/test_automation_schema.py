"""Integration test: automation schema migration idempotence.

Implements task 1.2 from ``.kiro/specs/p0-critical-path/tasks.md``.

Validates two invariants:

1. **Migration idempotence (Requirements 1.1, 1.8, 1.9)**: Running
   ``00_schemas.sql`` followed by ``10_automation.sql`` twice in
   succession on a fresh Postgres instance produces exactly the same
   set of tables, constraints, and indexes — no duplicates, no errors.

2. **Referential integrity and CHECK constraints**: Foreign key CASCADE
   deletes propagate correctly, and CHECK constraints reject invalid
   values with appropriate errors.

Gating
------

The test is gated behind ``--run-docker`` (registered in
``tests/conftest.py``). Without the flag the test is skipped so the
default fast-lane suite stays self-contained.

Lifecycle
---------

The test spins up a dedicated ``postgres:16-alpine`` container with a
random host port, runs the init scripts via ``psql``, asserts schema
state, then tears down the container. Each test function gets a fresh
database state via the session-scoped container fixture.
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

#: Docker image for the test Postgres instance.
PG_IMAGE: str = "postgres:16-alpine"

#: Postgres superuser credentials for the test container.
PG_USER: str = "test_user"
PG_PASSWORD: str = "test_pass"
PG_DB: str = "test_db"

#: Maximum time to wait for Postgres to accept connections.
PG_READY_TIMEOUT: float = 30.0

#: Polling interval for readiness checks.
POLL_INTERVAL: float = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Returns True iff Docker CLI is on PATH and daemon responds."""
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
    """Poll pg_isready inside the container until it succeeds or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "pg_isready", "-U", PG_USER, "-d", PG_DB,
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _run_sql_file(container_name: str, sql_path: Path) -> subprocess.CompletedProcess:
    """Execute a SQL file inside the Postgres container via psql."""
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
        check=False,
    )


def _run_sql(container_name: str, sql: str) -> subprocess.CompletedProcess:
    """Execute a SQL statement inside the Postgres container."""
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
        check=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container(request: pytest.FixtureRequest, repo_root: Path):
    """Start a fresh Postgres container for the test module.

    Yields the container name. Tears down on exit.
    """
    if not request.config.getoption("--run-docker"):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker to enable."
        )

    if not _docker_available():
        pytest.skip("Docker daemon not reachable; cannot run Postgres test.")

    container_name = f"test-automation-schema-{uuid.uuid4().hex[:8]}"

    # Start container
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

    # Wait for readiness
    assert _wait_for_pg(container_name, PG_READY_TIMEOUT), (
        f"Postgres container {container_name} did not become ready "
        f"within {PG_READY_TIMEOUT}s"
    )

    yield container_name

    # Teardown
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        check=False,
    )


@pytest.fixture()
def sql_paths(repo_root: Path) -> tuple[Path, Path]:
    """Return paths to the schema bootstrap and automation migration scripts."""
    schemas_sql = repo_root / "infra" / "postgres" / "00_schemas.sql"
    automation_sql = repo_root / "infra" / "postgres" / "10_automation.sql"
    assert schemas_sql.is_file(), f"Missing: {schemas_sql}"
    assert automation_sql.is_file(), f"Missing: {automation_sql}"
    return schemas_sql, automation_sql


# ---------------------------------------------------------------------------
# Tests: Migration Idempotence
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMigrationIdempotence:
    """Verify that running the init scripts twice produces no errors and
    leaves exactly one instance of each table, constraint, and index."""

    def test_double_run_no_errors(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """Running 00_schemas.sql + 10_automation.sql twice succeeds
        without errors (IF NOT EXISTS semantics)."""
        schemas_sql, automation_sql = sql_paths

        # First run
        r1 = _run_sql_file(pg_container, schemas_sql)
        assert r1.returncode == 0, f"First run 00_schemas.sql failed: {r1.stderr}"

        r2 = _run_sql_file(pg_container, automation_sql)
        assert r2.returncode == 0, f"First run 10_automation.sql failed: {r2.stderr}"

        # Second run (idempotence)
        r3 = _run_sql_file(pg_container, schemas_sql)
        assert r3.returncode == 0, f"Second run 00_schemas.sql failed: {r3.stderr}"

        r4 = _run_sql_file(pg_container, automation_sql)
        assert r4.returncode == 0, f"Second run 10_automation.sql failed: {r4.stderr}"

    def test_tables_single_instance_after_double_run(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """After two runs, each table exists exactly once in the
        automation schema."""
        schemas_sql, automation_sql = sql_paths

        # Run twice
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)

        # Query tables in automation schema
        result = _run_sql(
            pg_container,
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'automation'
            ORDER BY table_name;
            """,
        )
        assert result.returncode == 0, f"Table query failed: {result.stderr}"

        tables = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        expected_tables = sorted([
            "departments",
            "department_bots",
            "department_project_keys",
            "department_space_keys",
            "repo_mappings",
            "processed_events",
            "work_items",
            # Foundation spec additions (R2.10, R7.7).
            "audit_events",
            "probe_artifacts",
        ])

        assert tables == expected_tables, (
            f"Expected tables {expected_tables}, got {tables}"
        )

    def test_constraints_single_instance_after_double_run(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """After two runs, CHECK and UNIQUE constraints exist exactly
        once per table (no duplicates from repeated CREATE TABLE IF NOT
        EXISTS)."""
        schemas_sql, automation_sql = sql_paths

        # Run twice
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)

        # Query all constraints in automation schema
        result = _run_sql(
            pg_container,
            """
            SELECT constraint_name, table_name, constraint_type
            FROM information_schema.table_constraints
            WHERE table_schema = 'automation'
            ORDER BY constraint_name;
            """,
        )
        assert result.returncode == 0, f"Constraint query failed: {result.stderr}"

        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        # Each constraint name should appear exactly once
        constraint_names = [line.split("|")[0].strip() for line in lines]
        duplicates = [
            name for name in set(constraint_names)
            if constraint_names.count(name) > 1
        ]
        assert not duplicates, (
            f"Duplicate constraints found after double migration run: {duplicates}"
        )

    def test_indexes_single_instance_after_double_run(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """After two runs, indexes exist exactly once (CREATE INDEX IF
        NOT EXISTS semantics)."""
        schemas_sql, automation_sql = sql_paths

        # Run twice
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)

        # Query indexes in automation schema
        result = _run_sql(
            pg_container,
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'automation'
            ORDER BY indexname;
            """,
        )
        assert result.returncode == 0, f"Index query failed: {result.stderr}"

        index_names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        duplicates = [
            name for name in set(index_names)
            if index_names.count(name) > 1
        ]
        assert not duplicates, (
            f"Duplicate indexes found after double migration run: {duplicates}"
        )

        # Verify the three explicit indexes exist
        expected_indexes = {
            "idx_processed_events_expires_at",
            "idx_work_items_issue_key",
            "idx_work_items_status",
        }
        actual_index_set = set(index_names)
        missing = expected_indexes - actual_index_set
        assert not missing, (
            f"Expected indexes missing: {missing}. "
            f"Present indexes: {sorted(actual_index_set)}"
        )

    def test_foreign_keys_present_after_double_run(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """After two runs, all FK constraints referencing departments(id)
        exist exactly once."""
        schemas_sql, automation_sql = sql_paths

        # Run twice
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)

        # Query FK constraints
        result = _run_sql(
            pg_container,
            """
            SELECT tc.constraint_name, tc.table_name, ccu.table_name AS ref_table
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
                AND tc.table_schema = ccu.table_schema
            WHERE tc.table_schema = 'automation'
              AND tc.constraint_type = 'FOREIGN KEY'
            ORDER BY tc.table_name, tc.constraint_name;
            """,
        )
        assert result.returncode == 0, f"FK query failed: {result.stderr}"

        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        # Tables that should have FK to departments
        fk_tables = {line.split("|")[1].strip() for line in lines}
        expected_fk_tables = {
            "department_bots",
            "department_project_keys",
            "department_space_keys",
            "repo_mappings",
            "work_items",
        }
        missing = expected_fk_tables - fk_tables
        assert not missing, (
            f"Tables missing FK to departments: {missing}. "
            f"Tables with FK: {sorted(fk_tables)}"
        )


# ---------------------------------------------------------------------------
# Tests: FK Cascade and CHECK Constraint Behavior
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestForeignKeyCascadeAndCheckConstraints:
    """Verify FK ON DELETE CASCADE propagation and CHECK constraint
    rejection with example INSERTs."""

    @pytest.fixture(autouse=True)
    def _setup_schema(
        self, pg_container: str, sql_paths: tuple[Path, Path]
    ) -> None:
        """Ensure schema is initialized and clean for each test."""
        schemas_sql, automation_sql = sql_paths
        _run_sql_file(pg_container, schemas_sql)
        _run_sql_file(pg_container, automation_sql)

        # Clean existing test data
        _run_sql(pg_container, """
            DELETE FROM automation.work_items;
            DELETE FROM automation.repo_mappings;
            DELETE FROM automation.department_space_keys;
            DELETE FROM automation.department_project_keys;
            DELETE FROM automation.department_bots;
            DELETE FROM automation.processed_events;
            DELETE FROM automation.departments;
        """)

    def test_fk_cascade_deletes_department_bots(
        self, pg_container: str
    ) -> None:
        """Deleting a department cascades to department_bots."""
        # Insert parent
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('test-dept', 'Test Department');
        """)
        # Insert child
        _run_sql(pg_container, """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref, account_id)
            VALUES ('test-dept', 'jira', 'vault:secret/test', 'acc-123');
        """)

        # Verify child exists
        result = _run_sql(pg_container, """
            SELECT COUNT(*) FROM automation.department_bots
            WHERE department_id = 'test-dept';
        """)
        assert result.stdout.strip() == "1"

        # Delete parent
        _run_sql(pg_container, """
            DELETE FROM automation.departments WHERE id = 'test-dept';
        """)

        # Verify cascade
        result = _run_sql(pg_container, """
            SELECT COUNT(*) FROM automation.department_bots
            WHERE department_id = 'test-dept';
        """)
        assert result.stdout.strip() == "0", (
            "FK CASCADE did not delete department_bots when parent was removed"
        )

    def test_fk_cascade_deletes_project_keys(
        self, pg_container: str
    ) -> None:
        """Deleting a department cascades to department_project_keys."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('cascade-dept', 'Cascade Test');
        """)
        _run_sql(pg_container, """
            INSERT INTO automation.department_project_keys
                (department_id, project_key)
            VALUES ('cascade-dept', 'PROJ1');
        """)

        # Delete parent
        _run_sql(pg_container, """
            DELETE FROM automation.departments WHERE id = 'cascade-dept';
        """)

        # Verify cascade
        result = _run_sql(pg_container, """
            SELECT COUNT(*) FROM automation.department_project_keys
            WHERE department_id = 'cascade-dept';
        """)
        assert result.stdout.strip() == "0"

    def test_fk_cascade_deletes_space_keys(
        self, pg_container: str
    ) -> None:
        """Deleting a department cascades to department_space_keys."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('space-dept', 'Space Test');
        """)
        _run_sql(pg_container, """
            INSERT INTO automation.department_space_keys
                (department_id, space_key)
            VALUES ('space-dept', 'SPACEX');
        """)

        _run_sql(pg_container, """
            DELETE FROM automation.departments WHERE id = 'space-dept';
        """)

        result = _run_sql(pg_container, """
            SELECT COUNT(*) FROM automation.department_space_keys
            WHERE department_id = 'space-dept';
        """)
        assert result.stdout.strip() == "0"

    def test_fk_cascade_deletes_repo_mappings(
        self, pg_container: str
    ) -> None:
        """Deleting a department cascades to repo_mappings."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('repo-dept', 'Repo Test');
        """)
        _run_sql(pg_container, """
            INSERT INTO automation.repo_mappings
                (department_id, bitbucket_workspace, bitbucket_repo, jira_project_key)
            VALUES ('repo-dept', 'myws', 'myrepo', 'PROJ');
        """)

        _run_sql(pg_container, """
            DELETE FROM automation.departments WHERE id = 'repo-dept';
        """)

        result = _run_sql(pg_container, """
            SELECT COUNT(*) FROM automation.repo_mappings
            WHERE department_id = 'repo-dept';
        """)
        assert result.stdout.strip() == "0"

    def test_fk_cascade_deletes_work_items(
        self, pg_container: str
    ) -> None:
        """Deleting a department cascades to work_items."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('work-dept', 'Work Test');
        """)
        _run_sql(pg_container, """
            INSERT INTO automation.work_items
                (workflow_id, department_id, issue_key, status)
            VALUES ('wf-001', 'work-dept', 'TEST-1', 'pending');
        """)

        _run_sql(pg_container, """
            DELETE FROM automation.departments WHERE id = 'work-dept';
        """)

        result = _run_sql(pg_container, """
            SELECT COUNT(*) FROM automation.work_items
            WHERE department_id = 'work-dept';
        """)
        assert result.stdout.strip() == "0"

    def test_check_constraint_rejects_invalid_department_mode(
        self, pg_container: str
    ) -> None:
        """CHECK constraint on departments.mode rejects invalid values."""
        result = _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name, mode)
            VALUES ('bad-mode', 'Bad Mode', 'invalid_mode');
        """)
        assert result.returncode != 0, (
            "Expected CHECK constraint violation for invalid mode"
        )
        assert "chk_departments_mode" in result.stderr or "check" in result.stderr.lower(), (
            f"Expected CHECK constraint error, got: {result.stderr}"
        )

    def test_check_constraint_rejects_invalid_bot_service(
        self, pg_container: str
    ) -> None:
        """CHECK constraint on department_bots.service rejects invalid values."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('svc-dept', 'Service Test');
        """)

        result = _run_sql(pg_container, """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref)
            VALUES ('svc-dept', 'github', 'vault:secret/gh');
        """)
        assert result.returncode != 0, (
            "Expected CHECK constraint violation for invalid service 'github'"
        )
        assert "chk_department_bots_service" in result.stderr or "check" in result.stderr.lower()

    def test_check_constraint_rejects_invalid_bot_deployment(
        self, pg_container: str
    ) -> None:
        """CHECK constraint on department_bots.deployment rejects invalid values."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('deploy-dept', 'Deploy Test');
        """)

        result = _run_sql(pg_container, """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref, deployment)
            VALUES ('deploy-dept', 'jira', 'vault:secret/j', 'on_premise');
        """)
        assert result.returncode != 0, (
            "Expected CHECK constraint violation for invalid deployment 'on_premise'"
        )
        assert "chk_department_bots_deployment" in result.stderr or "check" in result.stderr.lower()

    def test_check_constraint_rejects_invalid_work_item_status(
        self, pg_container: str
    ) -> None:
        """CHECK constraint on work_items.status rejects invalid values."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('status-dept', 'Status Test');
        """)

        result = _run_sql(pg_container, """
            INSERT INTO automation.work_items
                (workflow_id, department_id, issue_key, status)
            VALUES ('wf-bad', 'status-dept', 'TEST-99', 'cancelled');
        """)
        assert result.returncode != 0, (
            "Expected CHECK constraint violation for invalid status 'cancelled'"
        )
        assert "chk_work_items_status" in result.stderr or "check" in result.stderr.lower()

    def test_check_constraint_accepts_valid_department_modes(
        self, pg_container: str
    ) -> None:
        """CHECK constraint on departments.mode accepts all valid values.

        Foundation spec migration (R3.2, R10.10): the legacy enum
        {active, shadow, paused, decommissioned} was replaced with
        {active, shadow, disabled} to match departments.schema.json.
        """
        valid_modes = ["active", "shadow", "disabled"]
        for mode in valid_modes:
            result = _run_sql(pg_container, f"""
                INSERT INTO automation.departments (id, display_name, mode)
                VALUES ('mode-{mode}', 'Mode {mode}', '{mode}');
            """)
            assert result.returncode == 0, (
                f"Valid mode '{mode}' was rejected: {result.stderr}"
            )

    def test_check_constraint_accepts_valid_bot_services(
        self, pg_container: str
    ) -> None:
        """CHECK constraint on department_bots.service accepts all valid values."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('valid-svc', 'Valid Services');
        """)

        valid_services = ["jira", "bitbucket", "confluence"]
        for service in valid_services:
            result = _run_sql(pg_container, f"""
                INSERT INTO automation.department_bots
                    (department_id, service, credential_ref)
                VALUES ('valid-svc', '{service}', 'vault:secret/{service}');
            """)
            assert result.returncode == 0, (
                f"Valid service '{service}' was rejected: {result.stderr}"
            )

    def test_check_constraint_accepts_valid_work_item_statuses(
        self, pg_container: str
    ) -> None:
        """CHECK constraint on work_items.status accepts all valid values."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('valid-status', 'Valid Status');
        """)

        valid_statuses = ["pending", "running", "completed", "failed"]
        for i, status in enumerate(valid_statuses):
            result = _run_sql(pg_container, f"""
                INSERT INTO automation.work_items
                    (workflow_id, department_id, issue_key, status)
                VALUES ('wf-valid-{i}', 'valid-status', 'TEST-{i}', '{status}');
            """)
            assert result.returncode == 0, (
                f"Valid status '{status}' was rejected: {result.stderr}"
            )

    def test_unique_constraint_department_bots_dept_service(
        self, pg_container: str
    ) -> None:
        """UNIQUE(department_id, service) prevents duplicate bot registrations."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('uniq-dept', 'Unique Test');
        """)
        _run_sql(pg_container, """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref)
            VALUES ('uniq-dept', 'jira', 'vault:secret/j1');
        """)

        # Attempt duplicate
        result = _run_sql(pg_container, """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref)
            VALUES ('uniq-dept', 'jira', 'vault:secret/j2');
        """)
        assert result.returncode != 0, (
            "Expected UNIQUE constraint violation for duplicate (dept, service)"
        )

    def test_unique_constraint_project_key(
        self, pg_container: str
    ) -> None:
        """UNIQUE on project_key prevents duplicate project key registrations."""
        _run_sql(pg_container, """
            INSERT INTO automation.departments (id, display_name)
            VALUES ('pk-dept1', 'PK Test 1');
            INSERT INTO automation.departments (id, display_name)
            VALUES ('pk-dept2', 'PK Test 2');
        """)
        _run_sql(pg_container, """
            INSERT INTO automation.department_project_keys
                (department_id, project_key)
            VALUES ('pk-dept1', 'UNIQ-KEY');
        """)

        # Attempt duplicate project_key from different department
        result = _run_sql(pg_container, """
            INSERT INTO automation.department_project_keys
                (department_id, project_key)
            VALUES ('pk-dept2', 'UNIQ-KEY');
        """)
        assert result.returncode != 0, (
            "Expected UNIQUE constraint violation for duplicate project_key"
        )

    def test_fk_rejects_orphan_bot_insert(
        self, pg_container: str
    ) -> None:
        """FK constraint rejects inserting a bot for a non-existent department."""
        result = _run_sql(pg_container, """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref)
            VALUES ('nonexistent-dept', 'jira', 'vault:secret/x');
        """)
        assert result.returncode != 0, (
            "Expected FK violation for non-existent department_id"
        )
        assert "foreign key" in result.stderr.lower() or "violates" in result.stderr.lower()
