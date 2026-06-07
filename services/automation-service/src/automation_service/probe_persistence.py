"""Probe-artifact persistence - partial-orphan handling (task 6.3).

The :class:`automation_service.probe.ProbeRunner` returns a
:class:`automation_service.probe.ProbeResult` whose ``state`` is
``"partial_orphan"`` when the write probe created an artifact but
failed to delete it (R5.3). The runner itself is DB-agnostic - it
emits the artifact descriptor on
``ProbeResult.artifact`` and lets the caller decide how to persist
it. This module ships the canonical INSERT helper so the admin
endpoints (task 5.6) and the probe-driven startup checks (task 6.2)
agree on the row layout.

The helper is kept intentionally small: every row maps 1:1 onto the
``automation.probe_artifacts`` columns declared in
``platform/infra/postgres/init/10_automation.sql``, and the function
returns ``True`` when a new row was inserted (vs ``False`` when the
result carried no orphan to record).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from db_shared import AsyncConnection

from .probe import ProbeArtifact, ProbeResult

__all__ = ["persist_partial_orphan"]

_LOG = logging.getLogger(__name__)


async def persist_partial_orphan(
    result: ProbeResult,
    conn: AsyncConnection,
    *,
    fallback_artifact: Optional[ProbeArtifact] = None,
) -> bool:
    """Insert a ``probe_artifacts`` row for *result* when it carries an orphan.

    The function is a no-op (returns ``False``) when the probe
    result's ``state`` is anything other than ``"partial_orphan"`` or
    when ``result.artifact`` is ``None``. In both cases there is no
    leftover artifact to record.

    Args:
        result: The :class:`ProbeResult` returned by
            :meth:`ProbeRunner.run`.
        conn: An open :class:`db_shared.AsyncConnection` (already
            inside a ``with_dept_session`` block - this helper does
            not start its own transaction).
        fallback_artifact: Optional override used when ``result``
            carries the ``"partial_orphan"`` state but the runner
            produced no artifact descriptor (legacy code paths
            constructed before R5.3 was wired up). Production code
            never relies on this fallback.

    Returns:
        ``True`` when a row was inserted, ``False`` otherwise.
    """

    if result.state != "partial_orphan":
        return False

    artifact = result.artifact or fallback_artifact
    if artifact is None:
        _LOG.warning(
            "probe.persist.skipped state=partial_orphan but artifact is None"
        )
        return False

    await conn.execute(
        """
        INSERT INTO automation.probe_artifacts
            (dept_id, service, artifact_type, external_id,
             title_or_name, state)
        VALUES ($1, $2, $3, $4, $5, 'partial_orphan')
        """,
        artifact.dept_id,
        artifact.service,
        artifact.artifact_type,
        artifact.external_id,
        artifact.title_or_name,
    )
    return True
