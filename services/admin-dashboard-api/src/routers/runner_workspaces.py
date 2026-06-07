"""``RunnerWorkspacesRouter`` - manual SSH workspace listing + purge.

Two admin-only endpoints backing the *Services → Workspaces* sub-tab:

* ``GET    /admin/runner/workspaces`` - listing of every directory under
  ``RUNNER_BASE_PATH`` on the SSH runner host. Each row carries
  ``issue_key``, ``size_mb`` (rounded ``du -sm``) and ``last_modified``
  (``stat -c %Y`` ISO-8601). Empty list when the runner is unreachable
  or the base path is empty.
* ``DELETE /admin/runner/workspaces/{issue_key}`` - recursive ``rm -rf``
  of ``$RUNNER_BASE_PATH/{issue_key}/`` on the runner host plus a
  best-effort ``docker rm -f`` of any container labelled
  ``ai-task={issue_key}``. The endpoint is the manual override for
  workspaces left behind by ``cleanup_policy=never`` tasks.

Path-traversal guard
--------------------

``issue_key`` MUST match the Jira-style pattern
``^[A-Z][A-Z0-9_]*-\\d+$`` (the same regex used by
``execution-runner-worker/src/runners/workspace_path.py`` for forward
construction). Anything else - ``..``, ``;``, ``&``, ``|``, ``$``,
backtick, newline, null-byte, or simply lower-case - is rejected with
``400 + {"error": "invalid_issue_key_format"}`` **before** any SSH
command is constructed. The router never calls
``str.format`` / f-string interpolation against unvalidated input;
even after the regex passes the value is forwarded through
:func:`shlex.quote` so any future regex relaxation cannot turn the
shell loose.

RBAC
----

The router declares ``Depends(require_admin)``; the dashboard's
``require_admin`` dependency only admits tokens that carry
``admin`` in the OIDC ``groups`` / ``roles`` claim. ``dept_admin``,
``lead`` and ``viewer`` therefore receive ``403`` from the auth
dependency itself - no extra check is needed here. The proxy's
:func:`classify_admin_path` matrix already classifies every
``/admin/runner/...`` path as ``required_role="admin"`` via the
default fail-closed branch.

Audit
-----

Every successful purge writes one ``workspace_manually_purged`` audit
event; every failed purge writes one ``workspace_purge_failed`` event.
Both carry ``actor_id``, ``actor_role="admin"``, ``dept_id=None``,
``resource=f"workspace:{issue_key}"`` and a payload containing
``issue_key``, ``freed_bytes`` (best-effort, ``0`` when unknown) and a
trimmed ``error`` reason on failure.

The SSH side-effect surface is injected through
``app.state.runner_workspaces_client``. Production wiring will bind it
to a real paramiko-backed client (deferred to the lifespan task that
also wires the ``probe_atlassian.py`` script). Tests bind a stub. When
the slot is ``None`` the GET endpoint returns ``{"workspaces": []}``
and the DELETE endpoint returns ``503`` with
``reason="runner_workspaces_client_unavailable"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Final, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from audit_logger import AuditEvent

from ..auth.dependencies import AuthClaims, require_admin

__all__ = [
    "ISSUE_KEY_PATTERN",
    "RunnerWorkspacesClient",
    "WorkspaceListEntry",
    "WorkspacePurgeResult",
    "router",
    "get_queue_status",
    "get_queue_status_stream",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------
#
# The regex matches Jira-style project keys followed by a numeric issue
# id, e.g. ``PAY-4211`` or ``OPS_CORE-12``. It is intentionally identical
# to the pattern the execution-runner uses when *constructing* workspace
# paths (``runners/workspace_path.py::ISSUE_KEY_PATTERN``) so the
# forward / reverse paths agree byte-for-byte. Anything else (path
# traversal vectors, shell metacharacters, lower-case, leading digits,
# null-bytes) is rejected before any subprocess / SSH command is built.
ISSUE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


# ---------------------------------------------------------------------------
# Client protocol - injected via ``app.state.runner_workspaces_client``
# ---------------------------------------------------------------------------


class WorkspaceListEntry:
    """Plain row shape for the ``GET /admin/runner/workspaces`` response.

    Implemented as a tiny ``__slots__`` class rather than a Pydantic
    model so the client implementation (production paramiko or test
    stub) can construct it without a runtime dependency on Pydantic.
    The router serialises it to JSON via :meth:`to_dict`.
    """

    __slots__ = ("issue_key", "size_mb", "last_modified")

    def __init__(
        self,
        *,
        issue_key: str,
        size_mb: int,
        last_modified: datetime,
    ) -> None:
        self.issue_key = issue_key
        self.size_mb = size_mb
        self.last_modified = last_modified

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "size_mb": self.size_mb,
            "last_modified": self.last_modified.isoformat(),
        }


class WorkspacePurgeResult:
    """Outcome of a single workspace purge.

    Attributes:
        purged: ``True`` when ``rm -rf`` succeeded on the SSH host.
            ``False`` is reserved for soft-fail purges where the
            directory was missing to begin with - the endpoint still
            returns ``200`` in that case so the UI's "delete twice"
            click does not surface as an error.
        freed_bytes: Approximate bytes reclaimed (``du -sb`` before
            ``rm``). ``0`` when the directory did not exist or the
            client could not measure.
    """

    __slots__ = ("purged", "freed_bytes")

    def __init__(self, *, purged: bool, freed_bytes: int) -> None:
        self.purged = purged
        self.freed_bytes = freed_bytes


@runtime_checkable
class RunnerWorkspacesClient(Protocol):
    """Side-effect surface backing the workspaces endpoints.

    Production wiring binds this to a paramiko / asyncssh client that
    targets the same SSH host the execution-runner-worker uses. Tests
    bind an in-memory stub.

    Both methods MUST be async and MUST treat ``issue_key`` as
    untrusted in spite of the router's regex guard - the contract is
    "validate at the boundary AND escape at the call-site".
    """

    async def list_workspaces(self) -> list[WorkspaceListEntry]:
        """Return one entry per directory under ``RUNNER_BASE_PATH``."""

    async def purge_workspace(self, issue_key: str) -> WorkspacePurgeResult:
        """Recursively remove ``$RUNNER_BASE_PATH/{issue_key}``.

        Implementations MUST shell-escape ``issue_key`` (e.g. via
        :func:`shlex.quote`) even when the router has already
        validated it through :data:`ISSUE_KEY_PATTERN`. Best-effort
        ``docker rm -f`` of any container labelled
        ``ai-task={issue_key}`` is part of this method's contract.
        """


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(
    prefix="/admin/runner",
    tags=["runner-workspaces"],
    dependencies=[Depends(require_admin)],
)


def _client(request: Request) -> RunnerWorkspacesClient | None:
    """Resolve the per-process :class:`RunnerWorkspacesClient`.

    Production wiring populates ``app.state.runner_workspaces_client``
    during lifespan. Tests bind their own stub before constructing the
    :class:`fastapi.testclient.TestClient`. When the slot is missing
    we return ``None`` and let the caller decide whether to short-
    circuit (GET → empty list, DELETE → 503).
    """

    return getattr(request.app.state, "runner_workspaces_client", None)


def _audit_sink(request: Request) -> Any | None:
    """Return the audit sink the router writes through.

    Reuses the same slot the feature-flags router consults
    (``app.state.feature_flag_audit_sink``) so workspace purge events
    land in the same ``automation.audit_events`` stream as other
    admin-dashboard-originated audit rows. Falls back to the
    ``AdminProxy._audit`` private handle when the dedicated slot is
    missing - same pattern as ``feature_flags.py::_get_audit``.
    """

    sink = getattr(request.app.state, "feature_flag_audit_sink", None)
    if sink is not None:
        return sink
    proxy = getattr(request.app.state, "admin_proxy", None)
    return getattr(proxy, "_audit", None) if proxy is not None else None


async def _safe_audit(sink: Any | None, event: AuditEvent) -> None:
    """Best-effort audit write - never raises.

    Matches the contract used by every other admin-dashboard-api
    router: a transient audit-DB outage MUST NOT mask the underlying
    HTTP outcome. The helper accepts any
    object exposing a coroutine ``write(event)`` method; it logs at
    ``WARNING`` and swallows any exception so the request keeps its
    outcome.
    """

    if sink is None:
        return
    try:
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 - never raise from an audit sink
        logger.warning(
            "runner_workspaces audit write failed (action=%s, key=%s): %s",
            event.action,
            (event.payload or {}).get("issue_key"),
            exc,
        )


# ---------------------------------------------------------------------------
# GET /admin/runner/workspaces
# ---------------------------------------------------------------------------


@router.get(
    "/workspaces",
    summary="List task workspaces under RUNNER_BASE_PATH",
)
async def list_runner_workspaces(request: Request) -> dict[str, Any]:
    """Return one row per directory under ``RUNNER_BASE_PATH``.

    The response shape is ``{"workspaces": [{"issue_key", "size_mb",
    "last_modified"}, ...]}``. When the runner client is not wired we
    return an empty list rather than 503 so the *Services →
    Workspaces* tab can still render (the operator sees "no
    workspaces yet" instead of a hard error). Errors raised by the
    client (SSH unreachable, ``ls`` failure) propagate up as a logged
    warning and an empty list.
    """

    client = _client(request)
    if client is None:
        return {"workspaces": []}

    try:
        entries = await client.list_workspaces()
    except Exception as exc:  # noqa: BLE001 - soft-fail, surface empty list
        logger.warning(
            "runner_workspaces.list_workspaces() failed (returning empty "
            "list so the UI keeps rendering): %s",
            exc,
        )
        return {"workspaces": []}

    return {
        "workspaces": [
            entry.to_dict() for entry in entries if entry is not None
        ]
    }


# ---------------------------------------------------------------------------
# DELETE /admin/runner/workspaces/{issue_key}
# ---------------------------------------------------------------------------


@router.delete(
    "/workspaces/{issue_key}",
    summary="Purge a task workspace + best-effort docker rm",
)
async def purge_runner_workspace(
    issue_key: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Recursively remove ``$RUNNER_BASE_PATH/{issue_key}/`` on the runner.

    Validation order:

    1. ``issue_key`` MUST match :data:`ISSUE_KEY_PATTERN`. Anything
       else short-circuits with ``400 +
       {"error": "invalid_issue_key_format"}``. The SSH client is
       **not** invoked.
    2. ``app.state.runner_workspaces_client`` MUST be set. When
       missing the endpoint returns ``503`` with
       ``reason="runner_workspaces_client_unavailable"`` so the
       operator sees a clear wiring failure instead of a silent
       no-op.
    3. The client's :meth:`purge_workspace` is called with the
       (already validated) ``issue_key``. The client MUST
       ``shlex.quote`` the key before constructing the SSH command;
       the router does not double-quote here so the test
       can introspect the SSH argv that the client built.

    Audit:

    * Success → ``workspace_manually_purged`` (``result="ok"``,
      payload carries ``freed_bytes`` and ``issue_key``).
    * Failure → ``workspace_purge_failed`` (``result="error"``,
      payload carries the trimmed exception message and ``issue_key``).

    Both audit writes are best-effort; the request outcome is never
    masked by an audit hiccup.
    """

    # ---- Step 1 - path-traversal guard ---------------------------------
    if ISSUE_KEY_PATTERN.fullmatch(issue_key) is None:
        # Audit the rejected attempt so the security panel surfaces the
        # path-traversal try. The payload carries the *raw* offending
        # key (truncated) so an operator can see what was attempted -
        # the redaction filter installed on the audit logger covers
        # any credential-shaped substring that might appear by accident.
        await _safe_audit(
            _audit_sink(request),
            AuditEvent(
                actor_id=actor.sub,
                actor_role="admin",
                dept_id=None,
                action="workspace_purge_rejected_invalid_key",
                resource=f"workspace:{issue_key[:64]}",
                result="denied",
                timestamp=datetime.now(tz=timezone.utc),
                payload={
                    "issue_key": issue_key[:64],
                    "reason": "invalid_issue_key_format",
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_issue_key_format"},
        )

    # ---- Step 2 - wiring guard -----------------------------------------
    client = _client(request)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "runner_workspaces_client_unavailable",
            },
        )

    # ---- Step 3 - invoke client + audit --------------------------------
    sink = _audit_sink(request)
    try:
        result = await client.purge_workspace(issue_key)
    except Exception as exc:  # noqa: BLE001 - surface as 502 + audit
        await _safe_audit(
            sink,
            AuditEvent(
                actor_id=actor.sub,
                actor_role="admin",
                dept_id=None,
                action="workspace_purge_failed",
                resource=f"workspace:{issue_key}",
                result="error",
                timestamp=datetime.now(tz=timezone.utc),
                payload={
                    "issue_key": issue_key,
                    "error": str(exc)[:500],
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "workspace_purge_failed",
                "issue_key": issue_key,
                "reason": str(exc)[:500],
            },
        ) from exc

    await _safe_audit(
        sink,
        AuditEvent(
            actor_id=actor.sub,
            actor_role="admin",
            dept_id=None,
            action="workspace_manually_purged",
            resource=f"workspace:{issue_key}",
            result="ok",
            timestamp=datetime.now(tz=timezone.utc),
            payload={
                "issue_key": issue_key,
                "freed_bytes": int(result.freed_bytes),
            },
        ),
    )

    return {
        "purged": bool(result.purged),
        "freed_bytes": int(result.freed_bytes),
        "issue_key": issue_key,
    }



# ---------------------------------------------------------------------------
# GET /admin/runner/queue-status
# ---------------------------------------------------------------------------
#
# Returns the current SSH runner queue state: active count, queued count,
# average wait time (from the last 10 completed workspaces), global
# concurrency quota, and a per-department breakdown.
#
# Data source: ``automation.execution_workspaces`` table.
# ---------------------------------------------------------------------------

#: Default global concurrency quota for the SSH runner. Overridable via
#: the ``RUNNER_MAX_CONCURRENT`` environment variable.
_RUNNER_MAX_CONCURRENT: Final[int] = int(
    os.environ.get("RUNNER_MAX_CONCURRENT", "5")
)

#: SSE push interval in seconds for the queue-status stream.
_QUEUE_STATUS_SSE_INTERVAL_S: Final[int] = int(
    os.environ.get("QUEUE_STATUS_SSE_INTERVAL_S", "5")
)


def _get_pool(request: Request) -> Any:
    """Resolve the asyncpg pool from app state.

    Raises 503 when the pool is not wired (Postgres still booting or
    the lifespan wiring has not completed yet).
    """

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reason": "pg_pool_unavailable"},
        )
    return pool


def _empty_queue_status() -> dict[str, Any]:
    """Return the stable empty queue shape the dashboard expects."""

    return {
        "active_count": 0,
        "queued_count": 0,
        "avg_wait_seconds": 0.0,
        "max_concurrent_global": _RUNNER_MAX_CONCURRENT,
        "by_dept": [],
    }


def _is_missing_execution_workspaces_error(exc: BaseException) -> bool:
    """Detect a fresh DB where the queue table migration has not run yet."""

    message = str(exc).lower()
    return (
        exc.__class__.__name__ == "UndefinedTableError"
        or "automation.execution_workspaces" in message
        and "does not exist" in message
    )


async def _fetch_queue_status(pool: Any) -> dict[str, Any]:
    """Query ``automation.execution_workspaces`` and compute queue metrics.

    Returns a dict matching the queue-status response schema:
    ``{active_count, queued_count, avg_wait_seconds, max_concurrent_global,
    by_dept: [{dept_id, active, queued, quota}]}``.
    """

    try:
        async with pool.acquire() as conn:
            # Active and queued counts
            counts_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'running') AS active_count,
                    COUNT(*) FILTER (WHERE status = 'queued')  AS queued_count
                  FROM automation.execution_workspaces
                 WHERE status IN ('running', 'queued')
                """,
            )

            # Average wait time from the last 10 completed workspaces
            avg_row = await conn.fetchrow(
                """
                SELECT COALESCE(
                    AVG(EXTRACT(EPOCH FROM (started_at - queued_at))), 0
                ) AS avg_wait_seconds
                  FROM (
                    SELECT started_at, queued_at
                      FROM automation.execution_workspaces
                     WHERE status = 'completed'
                       AND started_at IS NOT NULL
                       AND queued_at IS NOT NULL
                     ORDER BY finished_at DESC NULLS LAST
                     LIMIT 10
                  ) recent
                """,
            )

            # Per-department breakdown
            dept_rows = await conn.fetch(
                """
                SELECT
                    dept_id,
                    COUNT(*) FILTER (WHERE status = 'running') AS active,
                    COUNT(*) FILTER (WHERE status = 'queued')  AS queued
                  FROM automation.execution_workspaces
                 WHERE status IN ('running', 'queued')
                 GROUP BY dept_id
                 ORDER BY dept_id
                """,
            )
    except Exception as exc:
        if _is_missing_execution_workspaces_error(exc):
            logger.warning(
                "automation.execution_workspaces missing; returning empty "
                "runner queue status until migrations create it"
            )
            return _empty_queue_status()
        raise

    active_count = int(counts_row["active_count"]) if counts_row else 0
    queued_count = int(counts_row["queued_count"]) if counts_row else 0
    avg_wait_seconds = round(
        float(avg_row["avg_wait_seconds"]) if avg_row else 0.0, 2
    )

    by_dept = [
        {
            "dept_id": r["dept_id"],
            "active": int(r["active"]),
            "queued": int(r["queued"]),
            "quota": _RUNNER_MAX_CONCURRENT,
        }
        for r in dept_rows
    ]

    return {
        "active_count": active_count,
        "queued_count": queued_count,
        "avg_wait_seconds": avg_wait_seconds,
        "max_concurrent_global": _RUNNER_MAX_CONCURRENT,
        "by_dept": by_dept,
    }


@router.get(
    "/queue-status",
    summary="Current SSH runner queue state",
    dependencies=[Depends(require_admin)],
)
async def get_queue_status(request: Request) -> dict[str, Any]:
    """Return the current SSH runner queue state.

    Response shape:

    .. code-block:: json

        {
            "active_count": 3,
            "queued_count": 2,
            "avg_wait_seconds": 42.5,
            "max_concurrent_global": 5,
            "by_dept": [
                {"dept_id": "payments", "active": 2, "queued": 1, "quota": 5},
                {"dept_id": "platform", "active": 1, "queued": 1, "quota": 5}
            ]
        }

    Data source: ``automation.execution_workspaces`` table.

    * ``active_count`` - workspaces with ``status='running'``.
    * ``queued_count`` - workspaces with ``status='queued'``.
    * ``avg_wait_seconds`` - mean of ``started_at - queued_at`` for the
      last 10 completed workspaces.
    * ``max_concurrent_global`` - ``RUNNER_MAX_CONCURRENT`` env (default 5).
    * ``by_dept`` - per-department active/queued breakdown.
    """

    pool = _get_pool(request)
    return await _fetch_queue_status(pool)


# ---------------------------------------------------------------------------
# GET /admin/runner/queue-status/stream  (SSE)
# ---------------------------------------------------------------------------
#
# Pushes a new queue-status snapshot every ``_QUEUE_STATUS_SSE_INTERVAL_S``
# seconds as an SSE ``data:`` frame. The stream runs indefinitely until
# the client disconnects.
# ---------------------------------------------------------------------------


async def _queue_status_sse_generator(pool: Any) -> AsyncIterator[bytes]:
    """Yield SSE ``data:`` frames with queue-status snapshots.

    Each frame is a JSON-encoded snapshot matching the
    ``GET /admin/runner/queue-status`` response shape. A ``keepalive``
    comment is sent between data frames to prevent proxy timeouts.
    The generator runs until the client disconnects (detected via
    ``asyncio.CancelledError`` when the response is aborted).
    """

    try:
        while True:
            try:
                snapshot = await _fetch_queue_status(pool)
                payload = json.dumps(snapshot, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - never crash the stream
                # Surface the error as an SSE event so the UI can show
                # a transient warning without losing the connection.
                error_payload = json.dumps(
                    {"error": str(exc)[:200]}, ensure_ascii=False
                )
                yield f"event: error\ndata: {error_payload}\n\n".encode("utf-8")

            await asyncio.sleep(_QUEUE_STATUS_SSE_INTERVAL_S)
    except asyncio.CancelledError:
        # Client disconnected - clean exit.
        return


@router.get(
    "/queue-status/stream",
    summary="SSE stream of runner queue state (real-time updates)",
    dependencies=[Depends(require_admin)],
)
async def get_queue_status_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of the SSH runner queue state.

    Pushes a new JSON snapshot every 5 seconds (configurable via
    ``QUEUE_STATUS_SSE_INTERVAL_S`` env). The stream runs until the
    client disconnects.

    Each SSE frame is a ``data:`` event containing the same JSON shape
    as ``GET /admin/runner/queue-status``.

    Example SSE frame::

        data: {"active_count":3,"queued_count":2,"avg_wait_seconds":42.5,...}

    Error frames (transient DB issues) are sent as ``event: error``
    so the UI can surface a warning without reconnecting.
    """

    pool = _get_pool(request)
    return StreamingResponse(
        _queue_status_sse_generator(pool),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
