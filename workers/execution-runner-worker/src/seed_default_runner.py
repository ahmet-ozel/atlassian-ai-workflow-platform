"""Seed the default SSH runner from the legacy SSH_HOST environment variable.

This module provides backward-compatibility migration logic for deployments
that still use the single ``SSH_HOST`` environment variable. When ``SSH_HOST``
is set at boot time, the seed function:

1. Inserts a ``runner_id='default'`` row into ``infrastructure.ssh_runners``
   (idempotent — ``ON CONFLICT DO NOTHING``).
2. Assigns **all** existing departments to the default runner
   (idempotent — ``ON CONFLICT DO NOTHING``).

When ``SSH_HOST`` is **not** set and the ``infrastructure.ssh_runners`` table
is empty, the function logs a ``runner_pool_empty`` warning so operators are
aware that no execution capability is available until runners are configured
via the admin panel.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _resolve_ssh_host() -> str | None:
    """Read SSH_HOST from environment (canonical single-runner contract).

    Resolution order mirrors ``src/activities/ssh_healthcheck.py``:
    1. SSH_HOST (canonical)
    2. SSH_HOST_1 (deprecated alias)
    3. None (not set)
    """
    host = os.environ.get("SSH_HOST", "").strip()
    if host:
        return host
    legacy = os.environ.get("SSH_HOST_1", "").strip()
    if legacy:
        logger.warning(
            "SSH_HOST_1 is deprecated — use SSH_HOST instead. "
            "See infrastructure.ssh_runners table for the new multi-runner model."
        )
        return legacy
    return None


async def seed_default_runner(pool) -> None:
    """Seed the default runner from SSH_HOST env at boot time.

    Parameters
    ----------
    pool : asyncpg.Pool
        An active asyncpg connection pool connected to the platform database.

    Behavior
    --------
    - If ``SSH_HOST`` (or deprecated ``SSH_HOST_1``) is set:
      - Inserts ``runner_id='default'`` into ``infrastructure.ssh_runners``
        with the resolved host, port 22, username 'ai-runner', and the
        canonical vault path. Uses ``ON CONFLICT DO NOTHING`` for idempotency.
      - Assigns all existing departments to the default runner with
        priority 100. Uses ``ON CONFLICT DO NOTHING`` so already-assigned
        departments are not duplicated.
    - If ``SSH_HOST`` is not set:
      - Checks whether ``infrastructure.ssh_runners`` has any rows.
      - If empty, logs a ``runner_pool_empty`` warning.
    """
    ssh_host = _resolve_ssh_host()

    if not ssh_host:
        # SSH_HOST not set; check if runner pool is empty.
        try:
            count = await pool.fetchval(
                "SELECT COUNT(*) FROM infrastructure.ssh_runners"
            )
            if count == 0:
                logger.warning(
                    "runner_pool_empty: SSH_HOST is not set and "
                    "infrastructure.ssh_runners table is empty. "
                    "No execution capability available until runners are "
                    "configured via the admin panel. "
                    "See: /admin/security/ssh-runners"
                )
        except Exception as exc:  # noqa: BLE001
            # Table might not exist yet if migration hasn't run
            logger.debug(
                "Could not check runner pool status (table may not exist yet): %s",
                exc,
            )
        return

    # SSH_HOST is set; seed the default runner.
    logger.info(
        "SSH_HOST is set (%s) — seeding default runner for backward compatibility. "
        "DEPRECATED: migrate to infrastructure.ssh_runners table via admin panel.",
        ssh_host,
    )

    try:
        await pool.execute(
            """
            INSERT INTO infrastructure.ssh_runners
                (runner_id, host, port, username, vault_path, status)
            VALUES
                ('default', $1, 22, 'ai-runner', 'vault:ssh/runners/default/active', 'active')
            ON CONFLICT (runner_id) DO NOTHING
            """,
            ssh_host,
        )
        logger.info("Default runner seeded (host=%s)", ssh_host)

        # Assign all existing departments to the default runner
        result = await pool.execute(
            """
            INSERT INTO infrastructure.dept_ssh_assignments (dept_id, runner_id, priority)
            SELECT id, 'default', 100 FROM automation.departments
            ON CONFLICT DO NOTHING
            """
        )
        logger.info(
            "Existing departments assigned to default runner: %s", result
        )
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: the worker can still start, but execution capability
        # may not be available until the admin configures runners manually.
        logger.error(
            "Failed to seed default runner from SSH_HOST=%s: %s. "
            "Execution capability may be unavailable until runners are "
            "configured via the admin panel.",
            ssh_host,
            exc,
        )
