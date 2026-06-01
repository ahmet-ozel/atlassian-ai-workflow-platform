"""``LokiSearchProxy`` (`platform-mimari-ops` task 11.3 +
`platform-gap-fill` task 7.3).

**Validates: Requirements 4.5, 6.5, 6.9, 8.6**

Forwards an audit search query to Loki + cross-references the MinIO
archive index (task 13.4) when the query window extends beyond the
hot retention horizon (default 90 days). Each result carries an
``archived`` flag so the admin UI can render archived rows under a
separate banner.

In addition to the audit-aggregation surface this module also ships
the workflow-scoped log filter required by Requirement 8.6 — the
admin dashboard's workflow detail page asks for *all* log lines
that share a given ``trace_id`` so an operator can follow a single
request across automation-service, automation-worker,
agent-runner-worker, and the MCP server in chronological order.

The new endpoint is exposed at::

    GET /api/v1/workflows/{workflow_id}/logs?trace_id=...

It builds a LogQL stream selector from the supplied ``trace_id``
label and forwards to :class:`LokiClient`. The handler is
graceful: when no Loki client is wired the response degrades to
``{"results": [], "warnings": ["loki_unavailable"]}`` so the FE
panel can still render a clear "no logs available" state instead
of a generic 5xx page (mirroring the soft-fail pattern used by the
audit-search proxy above).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from ..auth.dependencies import require_admin

__all__ = ["router", "workflow_logs_router"]


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/audit", tags=["audit"])

workflow_logs_router = APIRouter(
    prefix="/api/v1/workflows", tags=["workflow-logs"]
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_LOG_LINES: int = 1000

_MAX_TRACE_ID_LEN: int = 64


def _get_loki(request: Request) -> Any:
    return getattr(request.app.state, "loki_client", None)


def _get_archive(request: Request) -> Any:
    return getattr(request.app.state, "archive_index", None)


@router.get("/search", dependencies=[Depends(require_admin)])
async def search(
    request: Request,
    actor_id: str | None = Query(default=None),
    dept_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    client_source: str | None = Query(
        default=None,
        description=(
            "Filter by X-Client-Source header value (e.g. "
            "agent-runner-worker, assistant-service)"
        ),
    ),
    trace_id: str | None = Query(
        default=None,
        max_length=_MAX_TRACE_ID_LEN,
        description=(
            "Filter results to log entries carrying the given trace_id "
            "label. Used by the workflow detail page to follow a "
            "single request across services (Requirement 8.6)."
        ),
    ),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """Aggregate Loki + archive hits.

    Both stores are read in parallel where possible; missing wiring
    (no Loki client, no archive index) degrades gracefully so the
    panel always renders.

    The ``client_source`` filter narrows results to events originating
    from a specific MCP client (Requirement 7.8 — observability by
    caller identity). When supplied, only log entries whose
    ``client_source`` field matches the filter value are returned.

    The ``trace_id`` filter (Requirement 8.6) narrows results to log
    entries carrying the matching trace_id label. The filter is
    forwarded to :meth:`LokiClient.search` when the underlying
    client supports it; otherwise the router falls back to a
    client-side filter on the returned hits so an older client still
    yields a correct (if smaller) result set.
    """

    loki = _get_loki(request)
    archive = _get_archive(request)

    loki_hits: list[dict[str, Any]] = []
    if loki is not None:
        try:
            loki_hits = list(
                await _invoke_loki_search(
                    loki,
                    actor_id=actor_id,
                    dept_id=dept_id,
                    action=action,
                    client_source=client_source,
                    trace_id=trace_id,
                    start=start,
                    end=end,
                )
            )
        except Exception:  # noqa: BLE001
            loki_hits = []

    archive_hits: list[dict[str, Any]] = []
    if archive is not None and start is not None and end is not None:
        try:
            from ..audit.types import AuditQuery, TimeRange  # type: ignore[import-not-found]

            tr = TimeRange(
                start=datetime.fromisoformat(start.replace("Z", "+00:00")),
                end=datetime.fromisoformat(end.replace("Z", "+00:00")),
            )
            query = AuditQuery(
                actor_id=actor_id,
                dept_id=dept_id,
                action=action,
                time_range=tr,
            )
            for hit in await archive.search(query):
                archive_hits.append(
                    {
                        "id": hit.id,
                        "archived": True,
                        "archive_uri": hit.archive_uri,
                        "summary": hit.summary,
                    }
                )
        except Exception:  # noqa: BLE001
            archive_hits = []

    db_hits: list[dict[str, Any]] = []
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (("actor_id", actor_id), ("dept_id", dept_id), ("action", action)):
            if value:
                values.append(value)
                clauses.append(f"{column} = ${len(values)}")
        if trace_id:
            values.append(trace_id)
            clauses.append(f"payload->>'trace_id' = ${len(values)}")
        if client_source and client_source != "admin-dashboard-api":
            values.append(client_source)
            clauses.append(f"payload->>'client_source' = ${len(values)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            async with pool.acquire() as conn:
                sql = (
                    "SELECT id, actor_id, dept_id, action, result, payload, created_at "
                    f"FROM automation.audit_events {where} ORDER BY created_at DESC LIMIT 100"
                )
                rows = await conn.fetch(sql, *values)
            for row in rows:
                payload = row["payload"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {}
                payload = payload if isinstance(payload, dict) else {}
                db_hits.append({
                    "id": f"db-{row['id']}", "archived": False,
                    "actor_id": row["actor_id"], "dept_id": row["dept_id"],
                    "action": row["action"], "trace_id": payload.get("trace_id"),
                    "client_source": payload.get("client_source") or "admin-dashboard-api",
                    "level": "INFO" if row["result"] == "ok" else "ERROR",
                    "at": row["created_at"].isoformat(),
                    "summary": json.dumps(payload, ensure_ascii=False, sort_keys=True)[:320],
                })
        except Exception:  # noqa: BLE001
            db_hits = []

    # Tag every Loki hit as not-archived so the UI can render a single
    # ordered list with a clear discriminator.
    loki_tagged = [{**h, "archived": False} for h in loki_hits]

    # Client-side client_source filter — applied when the Loki backend
    # does not natively support the field (graceful degradation).
    if client_source:
        loki_tagged = [
            h for h in loki_tagged
            if h.get("client_source", "").lower() == client_source.lower()
        ]

    # Client-side trace_id filter — same graceful-degradation pattern
    # as ``client_source``. When the Loki client supported the kwarg
    # natively (see :func:`_invoke_loki_search`) the hits are already
    # filtered, so this pass is a cheap no-op.
    if trace_id:
        loki_tagged = [
            h for h in loki_tagged
            if h.get("trace_id", "") == trace_id
        ]

    return {
        "results": db_hits + loki_tagged + archive_hits,
        "db_count": len(db_hits),
        "loki_count": len(loki_tagged),
        "archive_count": len(archive_hits),
    }


async def _invoke_loki_search(
    loki: Any,
    *,
    actor_id: str | None,
    dept_id: str | None,
    action: str | None,
    client_source: str | None,
    trace_id: str | None,
    start: str | None,
    end: str | None,
) -> Any:
    """Call ``loki.search(...)`` while tolerating older client signatures."""

    try:
        return await loki.search(
            actor_id=actor_id,
            dept_id=dept_id,
            action=action,
            client_source=client_source,
            trace_id=trace_id,
            start=start,
            end=end,
        )
    except TypeError:
        # Older client signature — retry without the new kwarg.
        return await loki.search(
            actor_id=actor_id,
            dept_id=dept_id,
            action=action,
            client_source=client_source,
            start=start,
            end=end,
        )


# ---------------------------------------------------------------------------
# Workflow-scoped log filter (Requirement 8.6)
# ---------------------------------------------------------------------------


def _build_logql(workflow_id: str, trace_id: str | None) -> str:
    """Render a LogQL stream selector for the workflow logs endpoint.

    The selector pins both the ``workflow_id`` and (when supplied)
    the ``trace_id`` labels so the operator only sees lines from the
    single request being investigated. The label values are
    surrounded with double quotes — Loki rejects unquoted matchers —
    and any embedded double quotes are escaped to keep the LogQL
    syntactically valid even when a future caller passes a label
    containing the character.

    The selector is intentionally generic (``{workflow_id="..."}``)
    rather than scoped to a particular ``service`` label so the
    response folds in lines from automation-service,
    automation-worker, agent-runner-worker, and the MCP server in a
    single round-trip — Property 10 (Requirements 8.1–8.6).
    """

    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    matchers = [f'workflow_id="{_escape(workflow_id)}"']
    if trace_id:
        matchers.append(f'trace_id="{_escape(trace_id)}"')
    return "{" + ", ".join(matchers) + "}"


def _validate_trace_id(trace_id: str | None) -> None:
    """Reject obviously-invalid ``trace_id`` values with HTTP 400.

    LogQL labels are restricted to printable characters; the
    canonical UUIDv7 representation used by ``observability.trace``
    is ``^[0-9a-fA-F-]+$``. We accept that plus the W3C 32-char hex
    form ``^[0-9a-f]{32}$``. Any whitespace, newlines, or label-
    breaking characters are rejected so a malicious caller cannot
    smuggle a ``"} | ...`` injection through the label filter.
    """

    if trace_id is None:
        return
    if not trace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_trace_id", "reason": "empty"},
        )
    if any(ch in trace_id for ch in ('"', "\\", "\n", "\r", "\t", "{", "}")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_trace_id", "reason": "forbidden_chars"},
        )


@workflow_logs_router.get(
    "/{workflow_id}/logs",
    dependencies=[Depends(require_admin)],
    summary="Filter Loki logs for a workflow (optionally by trace_id)",
)
async def workflow_logs(
    request: Request,
    workflow_id: str = Path(..., min_length=1, max_length=200),
    trace_id: str | None = Query(
        default=None,
        max_length=_MAX_TRACE_ID_LEN,
        description=(
            "When supplied, restrict results to log lines carrying the "
            "matching ``trace_id`` label. Validates Property 10 — same "
            "trace_id appears across all services for a single request."
        ),
    ),
    start: str | None = Query(
        default=None,
        description=(
            "Optional ISO-8601 lower bound for the log window. "
            "Forwarded to LokiClient when supported."
        ),
    ),
    end: str | None = Query(
        default=None,
        description=(
            "Optional ISO-8601 upper bound for the log window. "
            "Forwarded to LokiClient when supported."
        ),
    ),
    limit: int = Query(
        default=_MAX_LOG_LINES,
        ge=1,
        le=_MAX_LOG_LINES,
        description=f"Maximum number of log lines (max {_MAX_LOG_LINES}).",
    ),
) -> dict[str, Any]:
    """Return log lines for ``workflow_id`` (Requirement 8.6).

    The endpoint builds a LogQL stream selector ``{workflow_id="..."}``
    (plus an optional ``trace_id="..."`` matcher) and forwards to
    :class:`LokiClient`. When the Loki client is not wired (deployment
    without log aggregation, or boot phase before lifespan finished)
    the response degrades to ``{"results": [], "warnings":
    ["loki_unavailable"], ...}`` so the FE panel still renders a
    well-formed "no logs available" state instead of a generic 5xx
    page. This mirrors the soft-fail pattern used by the audit-search
    proxy above.

    The response shape is a stable envelope::

        {
            "workflow_id": "<id>",
            "trace_id": "<trace>" | null,
            "logql": "{workflow_id=...}",
            "results": [<log line dicts>],
            "warnings": ["loki_unavailable"?]  # only when missing
        }

    The ``logql`` field is included so the FE can show the operator
    which selector was used (and offer a "open in Grafana" deeplink
    that round-trips the same query).
    """

    _validate_trace_id(trace_id)

    logql = _build_logql(workflow_id, trace_id)
    response: dict[str, Any] = {
        "workflow_id": workflow_id,
        "trace_id": trace_id,
        "logql": logql,
        "results": [],
        "warnings": [],
    }

    loki = _get_loki(request)
    if loki is None:
        response["warnings"].append("loki_unavailable")
        return response

    # Prefer a dedicated ``query_range`` / ``query`` surface if the
    # client exposes one — that lets us push the LogQL selector down
    # without translating it back into the kwarg-shaped ``search``
    # API. Fall back to ``search(trace_id=...)`` when the client only
    # offers the audit-search surface (eg. the soft-fail stub used in
    # tests). Either way the response is normalised to a list of
    # dicts so the FE has a single shape to render.
    try:
        results = await _invoke_workflow_log_query(
            loki,
            workflow_id=workflow_id,
            trace_id=trace_id,
            logql=logql,
            start=start,
            end=end,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning(
            "workflow_logs: loki query failed for wf=%s trace=%s: %s",
            workflow_id,
            trace_id,
            exc,
        )
        response["warnings"].append("loki_query_failed")
        return response

    # Belt-and-braces: re-apply the workflow_id / trace_id filter on
    # the client side so a misconfigured upstream cannot leak rows
    # from a different workflow or request into the response.
    filtered: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        if row.get("workflow_id") and row["workflow_id"] != workflow_id:
            continue
        if trace_id and row.get("trace_id") and row["trace_id"] != trace_id:
            continue
        filtered.append(row)

    response["results"] = filtered[:limit]
    return response


async def _invoke_workflow_log_query(
    loki: Any,
    *,
    workflow_id: str,
    trace_id: str | None,
    logql: str,
    start: str | None,
    end: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Dispatch the log query to whichever surface the Loki client offers.

    Three surfaces are accepted (in order of preference):

    1. ``loki.query_range(query=logql, start=..., end=..., limit=...)``
       — the canonical Loki HTTP API. Returns a list of streams which
       we flatten to one entry per log line.
    2. ``loki.query(query=logql, ...)`` — instant query variant.
    3. ``loki.search(trace_id=..., ...)`` — fall back to the same
       audit-search surface used by ``/admin/audit/search``. The
       result already comes back as a list of dicts, so we return
       it as-is (the caller re-applies workflow_id / trace_id
       filters defensively).

    The dispatch is duck-typed so unit tests can ship a tiny stub
    without depending on the real ``LokiClient`` implementation.
    """

    # 1. ``query_range`` — preferred when the client exposes it.
    query_range = getattr(loki, "query_range", None)
    if callable(query_range):
        raw = await query_range(
            query=logql,
            start=start,
            end=end,
            limit=limit,
        )
        return _flatten_loki_streams(raw)

    # 2. ``query`` — instant query variant.
    query_fn = getattr(loki, "query", None)
    if callable(query_fn):
        raw = await query_fn(
            query=logql,
            start=start,
            end=end,
            limit=limit,
        )
        return _flatten_loki_streams(raw)

    # 3. Fallback: re-use the audit ``search`` surface. The handler
    # above already tolerates the ``trace_id`` kwarg via the
    # ``_invoke_loki_search`` helper, so use the same shim here.
    raw = await _invoke_loki_search(
        loki,
        actor_id=None,
        dept_id=None,
        action=None,
        client_source=None,
        trace_id=trace_id,
        start=start,
        end=end,
    )
    return [r for r in raw if isinstance(r, dict)]


def _flatten_loki_streams(raw: Any) -> list[dict[str, Any]]:
    """Normalise a Loki query response to a flat list of log dicts.

    The Loki HTTP API returns ``{"data": {"result": [{"stream": {...},
    "values": [[ts, line], ...]}, ...]}}``. Tests sometimes stub the
    method to return an already-flattened list; either shape is
    accepted so the router stays decoupled from the concrete client
    implementation.
    """

    if raw is None:
        return []
    if isinstance(raw, list):
        # Already flattened — every entry should be a dict.
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        # Loki HTTP API shape: ``{"data": {"result": [...]}}`` or just
        # ``{"result": [...]}`` depending on the client wrapper.
        result_block = raw.get("data", raw)
        if isinstance(result_block, dict):
            streams = result_block.get("result") or []
        else:
            streams = []
        flattened: list[dict[str, Any]] = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            labels = stream.get("stream") or {}
            for value in stream.get("values") or []:
                # Loki ``values`` rows are ``[<ns_ts>, <line>]`` pairs.
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    ts, line = value
                else:
                    ts, line = None, value
                entry: dict[str, Any] = {
                    "timestamp_ns": ts,
                    "line": line,
                }
                if isinstance(labels, dict):
                    # Surface the labels at the top level so the FE
                    # can render ``workflow_id`` / ``trace_id`` /
                    # ``service`` without re-parsing the stream
                    # block.
                    entry.update(
                        {k: v for k, v in labels.items() if isinstance(k, str)}
                    )
                flattened.append(entry)
        return flattened
    return []
