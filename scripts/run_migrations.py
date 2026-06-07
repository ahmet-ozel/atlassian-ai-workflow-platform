"""CLI entrypoint for the ``make migrate`` Makefile target (K1).

Applies every un-applied SQL migration in ``infra/postgres/migrations/``
against the configured Postgres DSN. Idempotent - safe to run repeatedly.

Usage:
    python scripts/run_migrations.py            # uses POSTGRES_DSN env
    python scripts/run_migrations.py --dsn ...  # explicit override
    python scripts/run_migrations.py --dir ...  # explicit migrations dir

Exit codes:
    0 - success (newly_applied may be 0, that's fine - it's idempotent)
    1 - migration failure
    2 - invalid CLI arguments
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# When invoked as a script, make ``libs/db-shared/src`` importable so we
# don't require ``pip install -e libs/db-shared`` first.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]  # platform/
sys.path.insert(0, str(_REPO_ROOT / "libs" / "db-shared" / "src"))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_migrations.py",
        description=(
            "Apply un-applied SQL migrations to the platform Postgres "
            "instance. Idempotent - safe to re-run."
        ),
    )
    p.add_argument(
        "--dsn",
        default=os.environ.get(
            "POSTGRES_DSN", "postgresql://ai:ai_dev_only@localhost:5433/ai"
        ),
        help=(
            "Postgres DSN. Defaults to POSTGRES_DSN env (or the dev compose "
            "default if env is unset)."
        ),
    )
    p.add_argument(
        "--dir",
        default=str(_REPO_ROOT / "infra" / "postgres" / "migrations"),
        help="Migrations directory (defaults to infra/postgres/migrations).",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return p


async def _run(dsn: str, migrations_dir: Path) -> int:
    """Apply migrations; return process exit code."""
    try:
        import asyncpg  # type: ignore[import-not-found]
    except ImportError:
        sys.stderr.write(
            "ERROR: asyncpg not installed. Activate the project venv or "
            "run inside the admin-dashboard-api container.\n"
        )
        return 1

    from db_shared.migrations import apply_migrations  # noqa: PLC0415

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        result = await apply_migrations(pool, migrations_dir)
    finally:
        await pool.close()

    print("=" * 60)
    print(f"Newly applied:        {len(result.newly_applied)}")
    print(f"Already applied:      {len(result.already_applied)}")
    print(f"Checksum mismatches:  {len(result.checksum_mismatches)}")
    print("=" * 60)
    for m in result.newly_applied:
        print(f"  + {m.version}  (checksum={m.checksum[:12]})")
    if result.checksum_mismatches:
        print("\nDRIFT DETECTED - these files were modified after apply:")
        for mm in result.checksum_mismatches:
            print(
                f"  ! {mm.version}  stored={mm.stored_checksum[:12]} "
                f"current={mm.current_checksum[:12]}"
            )
        # Mismatches don't fail the run (we don't re-apply mutable
        # migrations), but they're a strong signal the operator should
        # investigate.
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    migrations_dir = Path(args.dir)
    if not migrations_dir.is_dir():
        sys.stderr.write(
            f"ERROR: migrations directory does not exist: {migrations_dir}\n"
        )
        return 2

    try:
        return asyncio.run(_run(args.dsn, migrations_dir))
    except Exception as exc:  # noqa: BLE001
        logging.exception("migration run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
