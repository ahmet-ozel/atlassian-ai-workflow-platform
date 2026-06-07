"""SQL migration runner - applies versioned ``*.sql`` files idempotently.

Postgres ``docker-entrypoint-initdb.d`` only runs top-level ``.sql`` files at
first boot and **does not recurse**
into subdirectories. The repo has migration files under
``infra/postgres/migrations/`` (and previously ``config/migrations/``)
that nothing applies on boot - so tables like ``infrastructure.ssh_runners``,
``llm_providers``, ``test_runs``, ``bootstrap_tokens`` don't exist on a
fresh deployment and the admin features that depend on them return 500.

This module provides :func:`apply_migrations` - a small, dependency-light
applier that:

1. Creates ``shared.schema_migrations(version text PRIMARY KEY,
   applied_at timestamptz NOT NULL DEFAULT now(), checksum text NOT NULL)``
   if missing.
2. Discovers ``*.sql`` files in the supplied directory, sorted lexically.
3. Hashes each file (SHA-256) and applies un-applied ones in a single
   transaction per file. Already-applied files (matched by ``version``
   filename) are skipped; mismatched checksums of already-applied
   migrations are logged as warnings but **do not re-apply** (mutable
   migration files would corrupt the version graph).
4. Records the applied version + checksum so subsequent runs are no-ops.

Designed to be safe to call:
* from a FastAPI lifespan startup (admin-dashboard-api) - runs on every
  boot, idempotent;
* from a ``make migrate`` CLI target - same code path;
* from a Setup Wizard step - exposes a structured ``AppliedMigration``
  result the UI can render.

Usage::

    from db_shared.migrations import apply_migrations
    result = await apply_migrations(pool, migrations_dir)
    # result.newly_applied: list[AppliedMigration]
    # result.already_applied: list[str]  (versions skipped)
    # result.checksum_mismatches: list[ChecksumMismatch]

The module deliberately uses raw asyncpg rather than SQLAlchemy so it
can run before the ORM session machinery is configured.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "AppliedMigration",
    "ChecksumMismatch",
    "MigrationResult",
    "MigrationError",
    "apply_migrations",
    "discover_migrations",
    "SCHEMA_MIGRATIONS_DDL",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """A migration that the runner just applied in this invocation.

    Attributes
    ----------
    version:
        Filename without the ``.sql`` extension (e.g. ``"013_ssh_runner_pool"``).
    path:
        Absolute path to the source file.
    checksum:
        Lowercase hex SHA-256 of the file contents.
    """

    version: str
    path: Path
    checksum: str


@dataclass(frozen=True, slots=True)
class ChecksumMismatch:
    """An already-applied migration whose file content changed on disk.

    Migrations are immutable by contract - the on-disk file should never
    change after it has been applied to any deployed database. A mismatch
    is logged as a warning so operators can investigate; the runner does
    **not** re-apply the migration to avoid corrupting downstream state.
    """

    version: str
    stored_checksum: str
    current_checksum: str


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Aggregate outcome of a single :func:`apply_migrations` invocation."""

    newly_applied: list[AppliedMigration] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    checksum_mismatches: list[ChecksumMismatch] = field(default_factory=list)

    @property
    def total_discovered(self) -> int:
        return (
            len(self.newly_applied)
            + len(self.already_applied)
            + len(self.checksum_mismatches)
        )


class MigrationError(RuntimeError):
    """Raised when a migration fails to apply.

    The message includes the version + a snippet of the SQL error so
    operators can correlate against the source file.
    """

    def __init__(self, version: str, cause: str) -> None:
        self.version = version
        self.cause = cause
        super().__init__(f"migration {version!r} failed: {cause}")


# ---------------------------------------------------------------------------
# Pool / connection protocol - keeps the runner asyncpg-flavoured but
# doesn't hard-import asyncpg so tests can swap a fake.
# ---------------------------------------------------------------------------


class _SupportsAcquire(Protocol):
    """Subset of ``asyncpg.Pool`` we depend on.

    The pool's ``acquire()`` is an async context manager yielding an
    asyncpg ``Connection`` that exposes ``transaction()``, ``execute()``
    and ``fetch()``.
    """

    def acquire(self) -> Any:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: DDL for the version-tracking table. Idempotent (``IF NOT EXISTS``).
#: Lives in the ``shared`` schema so it joins the other audit / cost
#: tables created by ``infra/postgres/20_ops.sql``.
SCHEMA_MIGRATIONS_DDL: str = """
CREATE SCHEMA IF NOT EXISTS shared;

CREATE TABLE IF NOT EXISTS shared.schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum   TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_migrations(migrations_dir: Path) -> list[Path]:
    """Return ``*.sql`` files in ``migrations_dir`` sorted lexically.

    Lexical sort matches the convention used by all existing migration
    files (zero-padded numeric prefix: ``001_...``, ``002_...``, ``012_...``).
    Files starting with ``_`` or ``.`` are skipped so operators can park
    in-progress drafts without affecting the apply order.
    """
    if not migrations_dir.is_dir():
        logger.warning(
            "apply_migrations: migrations directory %s does not exist; "
            "nothing to apply",
            migrations_dir,
        )
        return []

    candidates = sorted(
        p
        for p in migrations_dir.glob("*.sql")
        if not p.name.startswith(("_", "."))
    )
    return candidates


def _checksum(path: Path) -> str:
    """SHA-256 of the file contents, lowercase hex."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _version_of(path: Path) -> str:
    """Drop ``.sql`` suffix; the filename is the version identifier."""
    return path.stem


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def apply_migrations(
    pool: _SupportsAcquire,
    migrations_dir: Path | str,
) -> MigrationResult:
    """Apply every un-applied migration file in ``migrations_dir``.

    The function is idempotent: re-running it after all migrations are
    applied is a no-op and returns a result with empty ``newly_applied``.

    Parameters
    ----------
    pool:
        An ``asyncpg.Pool`` (or anything implementing :class:`_SupportsAcquire`).
    migrations_dir:
        Directory containing the ``*.sql`` files. Files are applied in
        sorted-filename order - the existing zero-padded numeric prefix
        on the migrations defines the dependency graph.

    Returns
    -------
    MigrationResult
        Structured summary of newly applied, already applied, and
        checksum-mismatched migrations.

    Raises
    ------
    MigrationError
        If any migration fails to apply. The aggregate result up to the
        failing migration is wrapped in the exception's ``__cause__``
        chain (asyncpg surfaces a detailed Postgres error there).
    """
    path = Path(migrations_dir)
    files = discover_migrations(path)
    logger.info(
        "apply_migrations: discovered %d migration file(s) in %s",
        len(files),
        path,
    )

    result = MigrationResult()

    async with pool.acquire() as conn:
        # 1. Make sure the tracking table exists. Wrapping in a
        #    transaction keeps the DDL atomic - important on PG where
        #    bare CREATE TABLE is transactional.
        async with conn.transaction():
            await conn.execute(SCHEMA_MIGRATIONS_DDL)

        # 2. Load the set of already-applied versions + checksums.
        rows = await conn.fetch(
            "SELECT version, checksum FROM shared.schema_migrations"
        )
        applied: dict[str, str] = {r["version"]: r["checksum"] for r in rows}

        # 3. Walk discovered files; apply un-applied ones.
        for migration_path in files:
            version = _version_of(migration_path)
            current = _checksum(migration_path)

            if version in applied:
                stored = applied[version]
                if stored != current:
                    logger.warning(
                        "apply_migrations: %s already applied but checksum "
                        "drifted (stored=%s current=%s) - NOT re-applying; "
                        "investigate by hand",
                        version,
                        stored,
                        current,
                    )
                    result.checksum_mismatches.append(
                        ChecksumMismatch(
                            version=version,
                            stored_checksum=stored,
                            current_checksum=current,
                        )
                    )
                else:
                    result.already_applied.append(version)
                continue

            # Un-applied - read SQL and execute in a transaction.
            sql = migration_path.read_text(encoding="utf-8")
            logger.info(
                "apply_migrations: applying %s (%d bytes, checksum=%s)",
                version,
                len(sql),
                current[:12],
            )

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO shared.schema_migrations "
                        "(version, checksum) VALUES ($1, $2)",
                        version,
                        current,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "apply_migrations: %s FAILED: %s",
                    version,
                    exc,
                    exc_info=True,
                )
                raise MigrationError(version, str(exc)) from exc

            result.newly_applied.append(
                AppliedMigration(
                    version=version,
                    path=migration_path,
                    checksum=current,
                )
            )

    logger.info(
        "apply_migrations: done - newly_applied=%d already_applied=%d "
        "checksum_mismatches=%d (total_discovered=%d)",
        len(result.newly_applied),
        len(result.already_applied),
        len(result.checksum_mismatches),
        result.total_discovered,
    )
    return result
