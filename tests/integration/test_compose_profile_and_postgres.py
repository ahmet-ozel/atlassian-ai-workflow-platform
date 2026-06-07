"""Integration smoke tests: Compose profile gating, Postgres init, and RLS.

This module hosts three distinct integration tests that share the same
``--run-docker``-gated boot strategy but own *independent* Compose
lifecycles so a failure in one never poisons another:

1. ``test_task_intake_profile_brings_up_service_and_postgres_schemas_exist``
 (the implementation of ``workspace``):

 * **Profile gating **: ``--profile task-intake up
 -d`` brings ``task-intake-service`` up alongside the default
 stack.
 * **Postgres init order **: The four schemas
 (``automation``, ``assistant``, ``shared``, ``temporal``) MUST
 exist inside the running Postgres container after a clean boot,
 which is the observable proxy for "00_schemas.sql ran before
 10/40/50/99_*.sql".

2. ``test_postgres_rls_isolates_dept_admin_sessions_across_departments``
 (the implementation of ``foundation work``):

 * **Postgres RLS dept isolation and 9.5)**:
 With two departments seeded in ``automation.departments`` and
 two ``automation.audit_events`` rows attached to them, a
 ``dept_admin`` session opened via ``db_shared.with_dept_session``
 SHALL see exactly its own department's rows and zero rows from
 the *other* department. The same session, when re-targeted at
 the second department, MUST flip its visibility window. An
 ``admin`` session (``app.current_role = 'admin'``) MUST see both
 rows because the policy's role bypass branch fires.

Gating
------

Both tests are opt-in via the ``--run-docker`` pytest flag (registered
in ``tests/conftest.py``). Without the flag they skip cleanly so the
default fast-lane suite stays self-contained and runs without a Docker
daemon. With the flag the suite additionally probes ``docker info`` to
confirm the daemon is reachable before booting any stack.

Lifecycles
----------

* The profile / init-order test owns a single
 ``docker compose --profile task-intake up -d``  ``down -v`` cycle
 and asserts on host-side health probes plus
 ``docker compose exec postgres psql`` schema enumeration.
* The RLS test owns a single ``docker compose up -d postgres``
 ``down -v`` cycle so it does not boot the application services
 (which require ``.env`` files staged from ``.env.example`` and pull
 upstream images from registries the test environment may not reach).
 ``down -v`` drops named volumes so each run executes the init
 scripts from scratch.

Neither test asserts on services whose readiness depends on a healthy
external network (``firecrawl``, ``opencode-sidecar``, ...); Compose's
own ``depends_on: service_healthy`` ordering is the source of truth.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------

#: Compose file path relative to the workspace root. Mirrors the implementation's
#: helper so both integration tests stay in lock-step on the file
#: location.
COMPOSE_FILE_REL: str = "infra/docker-compose.yml"

#: Profile name that gates ``task-intake-service`` .
TASK_INTAKE_PROFILE: str = "task-intake"

#: Maximum wall-clock time to wait for every probed endpoint and for
#: Postgres to become queryable. The first boot of a fresh stack pulls
#: images and runs Postgres / Temporal init scripts, so the timeout
#: leaves room for slow first-time startup.
BOOT_TIMEOUT_SECONDS: float = 180.0

#: Polling cadence between health probes. 2s keeps load on the docker
#: daemon negligible without making the test wall-clock dominated by
#: sleep latency.
POLL_INTERVAL_SECONDS: float = 2.0

#: The four schemas the invariant / mandate after init.
#: The order matches the numeric prefix on ``infra/postgres/*.sql``:
#: 00_schemas.sql creates them; 10/40/50/99_*.sql consume them.
EXPECTED_SCHEMAS: frozenset[str] = frozenset(
    {"automation", "assistant", "shared", "temporal"}
)


@dataclass(frozen=True)
class HealthEndpoint:
    """A single host-side health probe.

 ``url`` is the URL the test polls from the host; ``service`` is the
 Compose service name the URL maps to (used purely for diagnostics
 in failure messages).
 """

    service: str
    url: str


#: The single profile-gated endpoint we assert on from the host. Other
#: services (``automation-service``, ``assistant-service``, etc.) are
#: covered by the implementation - repeating those probes here would just
#: lengthen the wall-clock without adding signal for the invariants
#: this task validates.
TASK_INTAKE_ENDPOINT: HealthEndpoint = HealthEndpoint(
    "task-intake-service", "http://localhost:8083/healthz"
)


#: Compose service  component path pairs whose ``env_file:`` directive
#: points at a ``.env`` file that does NOT ship with the repo (only
#: ``.env.example`` does, per . The test stages
#: each file by copying ``.env.example``  ``.env`` before bringing up
#: the stack and removes only the files it created on cleanup.
ENV_FILE_TARGETS: tuple[str, ...] = (
    "services/automation-service",
    "services/assistant-service",
    "services/admin-dashboard-api",
    "services/task-intake-service",
    "ui/admin-dashboard",
    "workers/agent-runner-worker",
    "workers/execution-runner-worker",
)


# ---------------------------------------------------------------------------
# Skip-gating helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Returns True iff a usable ``docker`` CLI is on PATH and the daemon
 responds to ``docker info``.

 We probe ``docker info`` instead of ``docker version`` because the
 latter succeeds even when the daemon is offline; ``docker info``
 requires a live daemon connection.
 """

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


# ---------------------------------------------------------------------------
# Env file staging
# ---------------------------------------------------------------------------


def _stage_env_files(repo_root: Path) -> list[Path]:
    """Copy ``.env.example``  ``.env`` for every Compose ``env_file:``
 target that is missing.

 Returns the list of ``.env`` files this call created so the
 teardown step can remove only those files (and not stomp on a
 user's pre-existing ``.env``).
 """

    created: list[Path] = []
    for target in ENV_FILE_TARGETS:
        component_dir = repo_root / target
        env_file = component_dir / ".env"
        env_example = component_dir / ".env.example"
        if env_file.exists():
            continue  # respect any pre-existing local override
        if not env_example.is_file():
            raise FileNotFoundError(
                f"missing .env.example for Compose env_file target: {env_example}"
            )
        env_file.write_bytes(env_example.read_bytes())
        created.append(env_file)
    return created


def _remove_staged_env_files(paths: list[Path]) -> None:
    """Best-effort cleanup of ``.env`` files staged by this test."""

    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Cleanup is best-effort; a leftover .env will be picked up
            # by the .gitignore rule (`*.env` per .
            pass


# ---------------------------------------------------------------------------
# Compose lifecycle helpers
# ---------------------------------------------------------------------------


def _compose_up_with_profile(
    repo_root: Path, profile: str
) -> subprocess.CompletedProcess:
    """Bring the stack up in detached mode with the given profile active.

 Activating ``--profile task-intake`` keeps every default-profile
 service in scope AND adds the ``task-intake-service`` (Compose's
 profile semantics: services without a ``profiles:`` key are always
 in scope, services with ``profiles:`` are added only when their
 profile is named on the CLI).
 """

    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_REL,
            "--profile",
            profile,
            "up",
            "-d",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _compose_down(repo_root: Path, profile: str) -> None:
    """Tear the stack down and drop named volumes.

 ``-v`` is required to drop ``pg_data`` / ``minio_data`` /
 ``agent_workspace`` so a subsequent run starts from a clean
 Postgres init-script state the init scripts
 only run on first boot of an empty data volume).

 The same ``--profile`` flag is passed on teardown so Compose
 considers the profile-gated container in its target set; without
 it, ``down`` may leave the gated container running.
 """

    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_REL,
            "--profile",
            profile,
            "down",
            "-v",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for_endpoint(
    endpoint: HealthEndpoint,
    timeout: float,
    interval: float,
) -> str | None:
    """Poll ``endpoint`` until it returns 2xx or the timeout expires.

 Returns ``None`` on success, or the last error string on failure.
 """

    import httpx  # local import to keep module import cheap when skipped

    deadline = time.monotonic() + timeout
    last_error: str = "not yet probed"

    while time.monotonic() < deadline:
        try:
            response = httpx.get(endpoint.url, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - any transport error means "not yet up"
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if 200 <= response.status_code < 300:
                return None
            last_error = (
                f"HTTP {response.status_code}: {response.text[:120]}"
            )
        time.sleep(interval)

    return last_error


def _list_postgres_schemas(
    repo_root: Path, timeout: float, interval: float
) -> tuple[set[str], str | None]:
    """Enumerate non-system schemas in the running ``postgres`` container.

 Polls because ``docker compose up -d`` returns before Postgres has
 finished running its init scripts even when the healthcheck is
 green - the ``service_healthy`` condition only enforces
 ``pg_isready``, which fires before ``00_schemas.sql`` has executed
 on a fresh data volume.

 Returns a ``(schemas, last_error)`` tuple. ``last_error`` is
 ``None`` on success.
 """

    deadline = time.monotonic() + timeout
    last_error: str = "not yet probed"

    # ``\dn`` lists schemas; we use a SQL query instead so the output
    # is parser-friendly and locale-independent.
    psql_query = (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name NOT IN ("
        "'pg_catalog','information_schema','pg_toast'"
        ") AND schema_name NOT LIKE 'pg_temp_%' "
        "AND schema_name NOT LIKE 'pg_toast_temp_%';"
    )

    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                COMPOSE_FILE_REL,
                "exec",
                "-T",  # no TTY - we want clean stdout for parsing
                "postgres",
                "psql",
                "-U",
                "ai",
                "-d",
                "ai",
                "-At",  # unaligned, tuples-only output
                "-c",
                psql_query,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            schemas = {
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip()
            }
            # Require all four expected schemas to be present before
            # declaring success - init scripts may run sequentially and
            # we want the steady state, not an intermediate snapshot.
            if EXPECTED_SCHEMAS.issubset(schemas):
                return schemas, None
            last_error = (
                f"schemas not yet present (have {sorted(schemas)!r}, "
                f"expected superset of {sorted(EXPECTED_SCHEMAS)!r})"
            )
        else:
            last_error = (
                f"`psql` exited {result.returncode}: "
                f"stderr={result.stderr.strip()[:200]}"
            )
        time.sleep(interval)

    return set(), last_error


def _is_service_running(repo_root: Path, service: str, profile: str) -> bool:
    """Return True iff Compose currently has at least one container for
 ``service`` running.

 ``compose ps -q <svc>`` prints container IDs on stdout; an empty
 string means the service is not running. Passing ``--profile`` is
 important: without it, profile-gated services are filtered out of
 the query and you can't observe their (non-)presence.
 """

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_REL,
            "--profile",
            profile,
            "ps",
            "-q",
            service,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_task_intake_profile_brings_up_service_and_postgres_schemas_exist(
    request: pytest.FixtureRequest, repo_root: Path
) -> None:
    """``--profile task-intake`` boots the gated service and Postgres
 init scripts created the four expected schemas.

 Validates and 16.4.

 The test is opt-in via ``--run-docker``. Without the flag (the
 default) it skips with a clear reason so CI fast-lanes don't pay
 for a Docker daemon spin-up.
 """

    if not request.config.getoption("--run-docker"):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker to enable."
        )

    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable on this host (`docker info` failed); "
            "cannot run Compose profile + Postgres smoke test."
        )

    compose_file = repo_root / COMPOSE_FILE_REL
    assert compose_file.is_file(), (
        f"Compose file missing at {compose_file}; cannot boot stack."
    )

    # Pre-flight sanity: the four init scripts whose ordering we are
    # validating must actually be present. If they were renamed or
    # deleted, the test would otherwise observe a vacuous "schemas not
    # present" failure that hides the real cause.
    pg_init_dir = repo_root / "infra" / "postgres"
    for script in (
        "00_schemas.sql",
        "10_automation.sql",
        "40_assistant.sql",
        "50_shared.sql",
        "99_temporal.sql",
    ):
        script_path = pg_init_dir / script
        assert script_path.is_file(), (
            f"Postgres init script missing at {script_path}; "
            f"The ordering check cannot run."
        )

    staged_envs = _stage_env_files(repo_root)
    try:
        # ---- Compose lifecycle: bring up with --profile task-intake -----
        up_result = _compose_up_with_profile(repo_root, TASK_INTAKE_PROFILE)
        assert up_result.returncode == 0, (
            "`docker compose --profile task-intake up -d` failed:\n"
            f"  stdout: {up_result.stdout}\n"
            f"  stderr: {up_result.stderr}"
        )

        # ---- Assertion 1: profile-gated service is reachable -----------
        last_error = _wait_for_endpoint(
            TASK_INTAKE_ENDPOINT,
            timeout=BOOT_TIMEOUT_SECONDS,
            interval=POLL_INTERVAL_SECONDS,
        )
        assert last_error is None, (
            f"task-intake-service did not become healthy within "
            f"{BOOT_TIMEOUT_SECONDS:.0f}s under --profile {TASK_INTAKE_PROFILE} "
            f"(URL {TASK_INTAKE_ENDPOINT.url}): {last_error}"
        )

        # Cross-check via Compose ps - even with the host probe green,
        # we want to confirm the service is owned by Compose under the
        # active profile (rather than, say, a stale container left
        # over from a previous run).
        assert _is_service_running(
            repo_root, "task-intake-service", TASK_INTAKE_PROFILE
        ), (
            "task-intake-service health probe succeeded but "
            "`docker compose ps -q task-intake-service` returned no "
            "container id; the service is not owned by this Compose "
            "lifecycle."
        )

        # ---- Assertion 2: Postgres init scripts created all schemas ----
        # Postgres init scripts run alphabetically under
        # /docker-entrypoint-initdb.d, so the numeric prefix
        # (00 < 10 < 40 < 50 < 99) defines a strict total order:
        # 00_schemas.sql creates the schemas before any of the
        # consumer scripts (10/40/50/99) reference them. The four
        # schemas being present at steady state is the observable
        # proxy that 00 ran first AND succeeded.
        schemas, schemas_error = _list_postgres_schemas(
            repo_root,
            timeout=BOOT_TIMEOUT_SECONDS,
            interval=POLL_INTERVAL_SECONDS,
        )
        assert schemas_error is None, (
            "Postgres did not converge to the expected schema set within "
            f"{BOOT_TIMEOUT_SECONDS:.0f}s: {schemas_error}"
        )
        missing = EXPECTED_SCHEMAS - schemas
        assert not missing, (
            f"Postgres init scripts did not create the expected schemas: "
            f"missing={sorted(missing)!r}, "
            f"observed={sorted(schemas)!r}. "
            f"This indicates 00_schemas.sql either failed or did not "
            f"run before 10/40/50/99_*.sql."
        )
    finally:
        _compose_down(repo_root, TASK_INTAKE_PROFILE)
        _remove_staged_env_files(staged_envs)


# ===========================================================================
# the implementation - Postgres RLS dept isolation
# ===========================================================================
# #
# The remaining helpers and the test below cover the RLS
# dept-isolation contract. They share ``--run-docker`` gating with the
# test above but own a separate, lighter Compose lifecycle
# (`up -d postgres` + `down -v`) so they can run without staging the
# application-service ``.env`` files.


#: Connection details for the Postgres container exposed on the host
#: by ``infra/docker-compose.yml`` (``ports: ["5432:5432"]``,
#: ``POSTGRES_USER=ai`` / ``POSTGRES_PASSWORD=ai_dev_only`` /
#: ``POSTGRES_DB=ai``). The integration test connects directly from
#: the host because asyncpg is the same driver the production
#: ``db-shared`` helper uses.
RLS_POSTGRES_HOST: str = "127.0.0.1"
RLS_POSTGRES_PORT: int = 5432
RLS_POSTGRES_USER: str = "ai"
RLS_POSTGRES_PASSWORD: str = "ai_dev_only"
RLS_POSTGRES_DB: str = "ai"

#: Two synthetic departments seeded into ``automation.departments``.
#: The IDs match the ``Department.id`` schema regex
#: (``^[a-z][a-z0-9-]{1,30}$``) so ``with_dept_session`` accepts them
#: without raising ``ValueError`` on the dept_id validator.
RLS_DEPT_A: str = "rls-test-alpha"
RLS_DEPT_B: str = "rls-test-bravo"

#: Non-superuser application role we create on the fly inside the
#: test container. RLS policies (even with ``FORCE ROW LEVEL
#: SECURITY``) are bypassed by superusers, so the dept_admin
#: visibility assertions MUST run as a non-superuser. The bootstrap
#: superuser ``ai`` (defined by ``POSTGRES_USER`` in
#: ``infra/docker-compose.yml``) is used only for the seed step,
#: which mirrors a future migration runner; the dept_admin
#: assertions run as ``rls_app`` so the policy is actually enforced.
RLS_APP_ROLE: str = "rls_app"
RLS_APP_PASSWORD: str = "rls_app_test_only"

#: Wall-clock timeout for waiting on the Postgres host port to accept
#: connections after ``docker compose up -d postgres`` returns. The
#: image is small (postgres:16-alpine) and the init scripts are
#: trivial, but a cold pull on a slow CI runner can push past 30s.
RLS_PG_READY_TIMEOUT_SECONDS: float = 120.0
RLS_PG_READY_INTERVAL_SECONDS: float = 1.5


def _require_docker_for_rls(request: pytest.FixtureRequest) -> None:
    """Skip the RLS test unless ``--run-docker`` is set and Docker is up.

 Mirrors ``_require_docker_or_skip`` in ``test_audit_round_trip.py``
 but additionally requires ``asyncpg`` and the local ``db_shared``
 package because the test calls into them directly.
 """

    if not request.config.getoption("--run-docker", default=False):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker "
            "to enable the Postgres RLS dept-isolation test."
        )
    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable on this host (`docker info` "
            "failed); cannot run the Postgres RLS dept-isolation test."
        )
    try:
        import asyncpg  # noqa: F401 - availability check
    except ImportError:
        pytest.skip(
            "asyncpg is not installed in this environment; the RLS "
            "dept-isolation test connects to Postgres directly and "
            "needs it. Install with `pip install asyncpg`."
        )
    try:
        from db_shared.session import with_dept_session  # noqa: F401
    except ImportError:
        pytest.skip(
            "db_shared.session.with_dept_session is not importable; "
            "ensure libs/db-shared/src is on sys.path (handled by "
            "tests/conftest.py)."
        )


def _compose_up_postgres_only(
    repo_root: Path,
) -> subprocess.CompletedProcess:
    """Boot just ``postgres`` without activating any optional profile.

 The default profile (no ``--profile`` flag) is sufficient because
 ``postgres`` carries no ``profiles:`` key in
 ``infra/docker-compose.yml``. Naming the service explicitly keeps the
 boot footprint to a single container so the test does not pull / start
 application images that require ``.env`` staging.
 """

    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_REL,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(int(RLS_PG_READY_TIMEOUT_SECONDS)),
            "postgres",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=RLS_PG_READY_TIMEOUT_SECONDS + 30,
    )


def _compose_down_with_volumes(repo_root: Path) -> None:
    """Tear the stack down including named volumes.

 ``-v`` is required so a subsequent run starts from a clean
 Postgres init-script state - critical for this test because the
 seeded departments / audit rows must not leak between runs and
 the RLS policies are wired up in ``10_automation.sql`` which only
 runs on first-boot of an empty data volume.
 """

    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_REL,
            "down",
            "-v",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


async def _wait_for_pg_ready(
    timeout: float, interval: float
) -> str | None:
    """Poll the host-exposed Postgres port until ``SELECT 1`` succeeds.

 ``docker compose up -d --wait`` already gates on the container's
 ``pg_isready`` healthcheck, but the moment the healthcheck flips
 green the init scripts may still be running on a fresh data
 volume. We additionally probe ``information_schema`` for the
 ``automation.departments`` table so the test does not race the
 init script execution.
 """

    import asyncpg

    deadline = time.monotonic() + timeout
    last_error = "not yet probed"
    dsn = (
        f"postgresql://{RLS_POSTGRES_USER}:{RLS_POSTGRES_PASSWORD}"
        f"@{RLS_POSTGRES_HOST}:{RLS_POSTGRES_PORT}/{RLS_POSTGRES_DB}"
    )

    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
        except Exception as exc:  # noqa: BLE001 - any error means "not yet ready"
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(interval)
            continue
        try:
            # The init scripts must have executed: the ``automation``
            # schema is created by 00_schemas.sql and the
            # ``departments`` table is created (with RLS enabled) by
            # 10_automation.sql. We probe both to confirm the state
            # we care about, not just connectivity.
            row = await conn.fetchrow(
                """
 SELECT 1 AS schema_present
 FROM information_schema.tables
 WHERE table_schema = 'automation'
 AND table_name = 'departments'
 """
            )
            if row is not None:
                return None
            last_error = (
                "automation.departments table not yet present "
                "(init scripts still running?)"
            )
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(interval)

    return last_error


async def _bootstrap_app_role(connection: Any) -> None:
    """Create the ``rls_app`` non-superuser role with table privileges.

 RLS - even with ``FORCE ROW LEVEL SECURITY`` - is bypassed by
 Postgres superusers. The bootstrap user
 ``POSTGRES_USER`` (``ai`` in ``infra/docker-compose.yml``) is a
 superuser, so a ``dept_admin`` session opened on its connection
 would ALWAYS see every row regardless of ``app.current_dept_id``
 - and the test would silently pass even if the policy were
 broken. To exercise the policy faithfully, the dept_admin
 assertions run as a non-superuser app role created on the fly.

 The role gets only the privileges the production application
 needs: ``USAGE`` on the ``automation`` schema and
 ``SELECT, INSERT, UPDATE, DELETE`` on the two RLS-protected
 tables. It does NOT receive ``BYPASSRLS`` so the policy is
 enforced on every query it issues.

 Idempotent: if the role already exists from a prior run that
 failed before teardown, we leave it in place rather than
 re-creating it.
 """

    role_exists = await connection.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = $1",
        RLS_APP_ROLE,
    )
    if role_exists is None:
        # The password is interpolated as a literal because asyncpg
        # does not bind parameters into DDL. ``RLS_APP_PASSWORD`` is
        # a hard-coded test-only constant so SQL-injection is not a
        # concern, but we still escape any single quote defensively
        # (the constant has none today, but a future maintainer might
        # change the literal).
        safe_password = RLS_APP_PASSWORD.replace("'", "''")
        await connection.execute(
            f"CREATE ROLE {RLS_APP_ROLE} LOGIN PASSWORD '{safe_password}'"
        )

    # Grants are idempotent - Postgres treats a re-grant as a no-op.
    # ``USAGE`` on the schema lets the role resolve table names;
    # CRUD on the two RLS tables lets the test exercise both the
    # SELECT and INSERT branches of the policy if a future variant
    # extends the assertion suite.
    await connection.execute(f"GRANT USAGE ON SCHEMA automation TO {RLS_APP_ROLE}")
    await connection.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "automation.departments, automation.audit_events "
        f"TO {RLS_APP_ROLE}"
    )
    # Future tables added under the automation schema will not be
    # auto-granted by the line above; that's intentional - the
    # production grant policy is also explicit per-table so a new
    # table cannot accidentally widen the app role's surface.


async def _drop_app_role(connection: Any) -> None:
    """Best-effort cleanup of the ``rls_app`` role created by the test.

 The Compose ``down -v`` invocation in the test's ``finally``
 block drops the data volume, so this cleanup is not strictly
 required - but dropping the role explicitly makes the test safe
 to re-run against a long-lived Postgres instance during local
 development (e.g. when iterating on the test itself with the
 Compose lifecycle pinned to a single boot).
 """

    try:
        await connection.execute(
            "REVOKE ALL ON automation.departments, automation.audit_events "
            f"FROM {RLS_APP_ROLE}"
        )
        await connection.execute(
            f"REVOKE USAGE ON SCHEMA automation FROM {RLS_APP_ROLE}"
        )
        await connection.execute(f"DROP ROLE IF EXISTS {RLS_APP_ROLE}")
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass


async def _seed_two_departments_and_audit_rows(connection: Any) -> None:
    """Seed ``RLS_DEPT_A`` and ``RLS_DEPT_B`` plus one audit row each.

 Runs on the bootstrap superuser connection so the seed step
 bypasses RLS entirely (mirroring how a future migration runner
 would seed the DB before the application takes over). The
 superuser bypass is intentional here - the dept_admin assertions
 run on a *separate* non-superuser connection where the policy is
 actually enforced.

 The seeded rows are intentionally minimal - just enough to make
 the isolation invariant observable. ``config_json`` is set to
 ``'{}'::jsonb`` because the schema requires NOT NULL but the
 test does not need a real department config.
 """

    # Two departments, distinguishable by id and display_name.
    await connection.execute(
        """
 INSERT INTO automation.departments
 (id, display_name, default_language, web_search_enabled,
 mode, config_json)
 VALUES
 ($1, $2, 'tr', false, 'active', '{}'::jsonb),
 ($3, $4, 'tr', false, 'active', '{}'::jsonb)
 """,
        RLS_DEPT_A,
        "RLS Test Alpha",
        RLS_DEPT_B,
        "RLS Test Bravo",
    )
    # One dept-scoped audit row per department. ``actor_role`` is
    # ``system`` so the CHECK constraint passes; the policy then
    # filters on dept_id when the dept_admin app-role session reads
    # the table.
    await connection.execute(
        """
 INSERT INTO automation.audit_events
 (actor_id, actor_role, dept_id, action, resource, result)
 VALUES
 ('seed-admin', 'system', $1, 'rls_seed', 'departments', 'ok'),
 ('seed-admin', 'system', $2, 'rls_seed', 'departments', 'ok')
 """,
        RLS_DEPT_A,
        RLS_DEPT_B,
    )


@pytest.mark.integration
def test_postgres_rls_isolates_dept_admin_sessions_across_departments(
    request: pytest.FixtureRequest, repo_root: Path
) -> None:
    """A ``dept_admin`` session sees its own dept's rows and nothing else.


 The test exercises the full RLS path:

 * ``infra/postgres/10_automation.sql`` enables ``ROW LEVEL
 SECURITY`` (and ``FORCE ROW LEVEL SECURITY``) on
 ``automation.departments`` and ``automation.audit_events`` and
 installs the ``dept_isolation`` / ``audit_dept_isolation``
 policies.
 * ``db_shared.with_dept_session`` runs ``SET LOCAL`` (via
 ``set_config(name, value, true)``) for ``app.current_dept_id``
 and ``app.current_role`` at the start of every transaction.
 * Inside a ``dept_admin`` session pinned to
 ``RLS_DEPT_A``: ``SELECT id FROM automation.departments`` MUST
 return exactly one row whose id is ``RLS_DEPT_A``; the
 ``RLS_DEPT_B`` row MUST be invisible. The same session must
 observe the same isolation on ``automation.audit_events``.
 * Re-opening the session pinned to ``RLS_DEPT_B`` MUST flip the
 visibility window so the assertion is symmetric.
 * An ``admin`` session MUST see both rows because the policy's
 role-bypass branch fires.

 The integration is end-to-end against a real Postgres container so
 drift between the SQL policy expressions and the helper's GUC
 names is caught - a unit test against a fake connection cannot
 observe a typo in ``app.current_dept_id`` because the fake never
 enforces RLS.
 """

    _require_docker_for_rls(request)

    compose_file = repo_root / COMPOSE_FILE_REL
    assert compose_file.is_file(), (
        f"Compose file missing at {compose_file}; cannot boot stack."
    )

    pg_init_dir = repo_root / "infra" / "postgres"
    for script in ("00_schemas.sql", "10_automation.sql"):
        script_path = pg_init_dir / script
        assert script_path.is_file(), (
            f"Postgres init script missing at {script_path}; the RLS "
            f"dept-isolation test depends on it executing on first boot."
        )

    # ---- Compose lifecycle: bring up only the postgres service -----
    up_result = _compose_up_postgres_only(repo_root)
    assert up_result.returncode == 0, (
        "`docker compose up -d --wait postgres` failed:\n"
        f"  stdout: {up_result.stdout}\n"
        f"  stderr: {up_result.stderr}"
    )

    try:
        # Even after ``--wait`` flips the healthcheck green, init
        # scripts may still be running on a fresh data volume. We
        # additionally probe for the ``automation.departments`` table
        # so the seed step does not race the SQL execution.
        ready_error = asyncio.run(
            _wait_for_pg_ready(
                timeout=RLS_PG_READY_TIMEOUT_SECONDS,
                interval=RLS_PG_READY_INTERVAL_SECONDS,
            )
        )
        assert ready_error is None, (
            "Postgres did not converge to a queryable state with "
            "automation.departments present within "
            f"{RLS_PG_READY_TIMEOUT_SECONDS:.0f}s: {ready_error}"
        )

        asyncio.run(_run_rls_isolation_assertions())
    finally:
        _compose_down_with_volumes(repo_root)


async def _run_rls_isolation_assertions() -> None:
    """Run the three-part isolation assertion suite on two connections.

 Two connections are required because:

 * Setup (role bootstrap + seed) runs on the bootstrap superuser
 ``ai`` so the INSERTs and ``CREATE ROLE`` succeed unconditionally
 regardless of RLS policies.
 * The dept_admin / admin visibility assertions run on a separate
 non-superuser ``rls_app`` connection so RLS is *actually
 enforced*. A superuser session would silently bypass the
 ``dept_isolation`` and ``audit_dept_isolation`` policies and
 the test would pass even if they were broken.

 Split out from the test entry point so the ``async`` body stays
 flat and the ``finally`` cleanup in the sync test wrapper does
 not need to nest event loops.
 """

    import asyncpg

    from db_shared.session import with_dept_session

    superuser_dsn = (
        f"postgresql://{RLS_POSTGRES_USER}:{RLS_POSTGRES_PASSWORD}"
        f"@{RLS_POSTGRES_HOST}:{RLS_POSTGRES_PORT}/{RLS_POSTGRES_DB}"
    )
    app_dsn = (
        f"postgresql://{RLS_APP_ROLE}:{RLS_APP_PASSWORD}"
        f"@{RLS_POSTGRES_HOST}:{RLS_POSTGRES_PORT}/{RLS_POSTGRES_DB}"
    )

    setup_conn = await asyncpg.connect(superuser_dsn, timeout=10)
    try:
        # ---- Bootstrap: create non-superuser role + grants ----------
        await _bootstrap_app_role(setup_conn)
        # ---- Seed: insert two depts + two audit rows (superuser) ----
        await _seed_two_departments_and_audit_rows(setup_conn)
    except BaseException:
        try:
            await setup_conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise

    # The setup connection is intentionally kept open until the end so
    # we can drop the app role on cleanup. The assertions themselves
    # run on a fresh app-role connection.
    app_conn = await asyncpg.connect(app_dsn, timeout=10)
    try:
        # ---- Assertion 1: dept_admin pinned to dept-A sees only A ----
        async with with_dept_session(
            "dept_admin", RLS_DEPT_A, connection=app_conn
        ):
            dept_rows = await app_conn.fetch(
                "SELECT id FROM automation.departments "
                "WHERE id IN ($1, $2) ORDER BY id",
                RLS_DEPT_A,
                RLS_DEPT_B,
            )
            visible_ids = [r["id"] for r in dept_rows]
            assert visible_ids == [RLS_DEPT_A], (
                "dept_admin session pinned to "
                f"{RLS_DEPT_A!r} must see exactly one departments row "
                f"({RLS_DEPT_A!r}); got {visible_ids!r}. "
                ": the "
                "dept_isolation policy is not filtering on "
                "current_setting('app.current_dept_id')."
            )
            assert RLS_DEPT_B not in visible_ids, (
                f"dept_admin session for {RLS_DEPT_A!r} leaked the "
                f"row of department {RLS_DEPT_B!r}; visible={visible_ids!r}."
            )

            audit_rows = await app_conn.fetch(
                """
 SELECT dept_id
 FROM automation.audit_events
 WHERE action = 'rls_seed'
 ORDER BY dept_id
 """
            )
            visible_dept_ids = [r["dept_id"] for r in audit_rows]
            assert visible_dept_ids == [RLS_DEPT_A], (
                "dept_admin session pinned to "
                f"{RLS_DEPT_A!r} must see exactly one audit_events "
                f"row (its own); got dept_ids={visible_dept_ids!r}. "
                ": the "
                "audit_dept_isolation policy is not filtering on "
                "current_setting('app.current_dept_id')."
            )

        # ---- Assertion 2: same connection, re-pinned to dept-B ------
        # The first ``with_dept_session`` block exited cleanly so its
        # ``SET LOCAL`` GUCs were rolled back with COMMIT. Opening a
        # second session with a different dept_id confirms the
        # policy reads the *current* GUC value, not a value cached
        # from the previous transaction on the same pooled connection.
        async with with_dept_session(
            "dept_admin", RLS_DEPT_B, connection=app_conn
        ):
            dept_rows = await app_conn.fetch(
                "SELECT id FROM automation.departments "
                "WHERE id IN ($1, $2) ORDER BY id",
                RLS_DEPT_A,
                RLS_DEPT_B,
            )
            visible_ids = [r["id"] for r in dept_rows]
            assert visible_ids == [RLS_DEPT_B], (
                "Re-pinning the same connection to "
                f"{RLS_DEPT_B!r} must flip visibility to that "
                f"department's row only; got {visible_ids!r}. "
                "GUC re-binding via with_dept_session is broken or "
                "the policy is caching a stale value."
            )

        # ---- Assertion 3: admin session sees both rows --------------
        # The dept_isolation policy has an OR branch on
        # ``current_setting('app.current_role', true) = 'admin'`` so
        # an admin session bypasses the dept filter (despite running
        # as the non-superuser ``rls_app`` connection - the bypass is
        # at the policy level, not the role level). Verifying this
        # negative case proves the test is observing real isolation
        # in assertions 1 and 2 rather than a global filter that
        # accidentally returned the right answer.
        async with with_dept_session("admin", None, connection=app_conn):
            dept_rows = await app_conn.fetch(
                "SELECT id FROM automation.departments "
                "WHERE id IN ($1, $2) ORDER BY id",
                RLS_DEPT_A,
                RLS_DEPT_B,
            )
            visible_ids = [r["id"] for r in dept_rows]
            assert set(visible_ids) >= {RLS_DEPT_A, RLS_DEPT_B}, (
                "admin session must bypass the dept filter and see "
                "both seeded departments; "
                f"got {visible_ids!r}, expected superset of "
                f"{[RLS_DEPT_A, RLS_DEPT_B]!r}. "
                "The dept_isolation policy's role-bypass branch "
                "appears broken."
            )

            audit_rows = await app_conn.fetch(
                """
 SELECT dept_id
 FROM automation.audit_events
 WHERE action = 'rls_seed'
 ORDER BY dept_id
 """
            )
            visible_dept_ids = sorted(r["dept_id"] for r in audit_rows)
            assert visible_dept_ids == sorted([RLS_DEPT_A, RLS_DEPT_B]), (
                "admin session must see both seeded audit rows; "
                f"got {visible_dept_ids!r}, expected "
                f"{sorted([RLS_DEPT_A, RLS_DEPT_B])!r}."
            )
    finally:
        try:
            await app_conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await _drop_app_role(setup_conn)
        except Exception:  # noqa: BLE001
            pass
        try:
            await setup_conn.close()
        except Exception:  # noqa: BLE001
            pass
