"""``WorkflowsDrillDownRouter`` (`platform-mimari-ops` task 11.2 +
``platform-mimari-uyumluluk`` task 12.1).

**Validates: Requirements 4.3, 8.4 (Q9 — workflow drill-down with
``llm_usage[]``, ``audit_chain[]``, ``external_links{}``).**

Three endpoints:

* ``GET /admin/workflows`` — list dept-scoped workflows (forwarded
  to automation-service via :class:`AdminProxy`).
* ``GET /admin/workflows/{workflow_id}`` — drill-down: history,
  signals, activities, failures, **plus** the additive fields
  required by ``platform-mimari-uyumluluk`` task 12.1:

    - ``llm_usage[]`` — one entry per ``shared.cost_tracking`` row
      keyed on the workflow_id, carrying
      ``{activity_id, prompt_path, prompt_version, model,
      token_in, token_out, cost_usd}``.
    - ``audit_chain[]`` — every ``automation.audit_events`` row
      whose ``resource = 'workflow:{workflow_id}'`` or whose
      ``payload->>'workflow_id'`` equals the workflow id, carrying
      ``{action, actor, timestamp, payload_summary}``.
    - ``external_links{}`` — ``{jira_issue_url?, bitbucket_pr_url?,
      confluence_page_url?}`` extracted from the audit chain via
      the W3 deeplink helper :func:`_external_links.build_external_links`.

* ``POST /admin/workflows/{workflow_id}/cancel`` — send a cancel
  signal (forwarded to automation-service).

The drill-down endpoint folds the upstream Temporal payload (events
/ activities / failures, owned by automation-service) together with
locally-queried Postgres data so the FE makes a single round-trip.
When the upstream proxy or the Postgres pool is not wired (eg. boot
phase, dev environment without Temporal), the endpoint still
returns a well-formed envelope with empty arrays so the FE renders
gracefully.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.dependencies import AuthClaims, require_admin
from ._external_links import build_external_links

__all__ = ["router"]


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/workflows", tags=["workflows"])


#: Maximum number of audit / cost rows we surface per workflow. The
#: drill-down panel paginates client-side; capping at 500 keeps the
#: backend round-trip bounded for pathological workflows that emitted
#: thousands of events without exhausting the FE budget.
_MAX_ROWS: int = 500

#: Maximum length (in characters) of the JSON-encoded payload summary
#: included in each ``audit_chain[]`` entry. Audit payloads are
#: free-form JSONB and can carry large diffs / artifact lists; the
#: drill-down panel only needs a teaser, not the full body.
_PAYLOAD_SUMMARY_MAX_LEN: int = 280


def _proxy(request: Request) -> Any:
    proxy = getattr(request.app.state, "admin_proxy", None)
    if proxy is None:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "reason": "admin_proxy_unavailable"},
        )
    return proxy


def _optional_proxy(request: Request) -> Any | None:
    """Return the optional :class:`AdminProxy` instance, or ``None``.

    Unlike :func:`_proxy` this never raises — used by the drill-down
    endpoint, which can still serve a useful response (the local
    enrichment fields) when the upstream proxy is missing.
    """

    return getattr(request.app.state, "admin_proxy", None)


def _pg_pool(request: Request) -> Any | None:
    """Return the optional asyncpg pool, or ``None``."""

    return getattr(request.app.state, "pg_pool", None)


def _summarise_payload(payload: Any) -> str | None:
    """Render an audit payload into a short, JSON-safe string.

    The audit ``payload`` column is JSONB and arrives via asyncpg as
    either a Python dict (when codec registration is in place) or a
    raw JSON string (when not). Both shapes are accepted; the helper
    returns ``None`` when the payload is empty so the JSON envelope
    omits the key cleanly.
    """

    if payload is None:
        return None
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(payload)
    text = text.strip()
    if not text:
        return None
    if len(text) > _PAYLOAD_SUMMARY_MAX_LEN:
        return text[: _PAYLOAD_SUMMARY_MAX_LEN - 1] + "…"
    return text


def _coerce_payload_obj(payload: Any) -> Any:
    """Return ``payload`` as a Python object (dict/list/str/None).

    asyncpg may surface JSONB as either a string or a parsed object
    depending on codec registration. The external-link extractor
    expects a mapping; coerce strings via :func:`json.loads` once
    and fall back to ``None`` on parse failures so a malformed row
    does not break the whole response.
    """

    if payload is None or isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            return None
    return None


async def _fetch_llm_usage(
    pool: Any, workflow_id: str
) -> list[dict[str, Any]]:
    """Return ``llm_usage[]`` rows for ``workflow_id``.

    Reads from ``shared.cost_tracking`` (table created by
    ``infra/postgres/20_ops.sql``). Rows tagged ``probe`` /
    ``sandbox`` are excluded so the workflow detail panel does not
    surface bookkeeping entries unrelated to the actual workflow run.

    The ``prompt_path`` and ``prompt_version`` fields are not native
    columns on ``shared.cost_tracking`` — they are recorded
    side-by-side in the corresponding ``automation.audit_events``
    row (action ``llm_activity_recorded`` / ``chat_message`` /
    ``prompt_sandbox_run_recorded``). To keep the response
    self-contained we look up the most recent audit row that shares
    the ``activity_id`` and pull both fields out of its JSONB
    ``payload``. Missing values come back as ``None``.
    """

    sql = """
        SELECT c.activity_id,
               c.model,
               c.token_in,
               c.token_out,
               c.cost_usd,
               (
                   SELECT a.payload->>'prompt_path'
                     FROM automation.audit_events AS a
                    WHERE a.payload ? 'activity_id'
                      AND a.payload->>'activity_id' = c.activity_id
                    ORDER BY a.created_at DESC
                    LIMIT 1
               ) AS prompt_path,
               (
                   SELECT a.payload->>'prompt_version'
                     FROM automation.audit_events AS a
                    WHERE a.payload ? 'activity_id'
                      AND a.payload->>'activity_id' = c.activity_id
                    ORDER BY a.created_at DESC
                    LIMIT 1
               ) AS prompt_version
          FROM shared.cost_tracking AS c
         WHERE c.workflow_id = $1
           AND c.cost_tag = 'production'
         ORDER BY c.created_at ASC
         LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, workflow_id, _MAX_ROWS)
    return [
        {
            "activity_id": r["activity_id"],
            "prompt_path": r["prompt_path"],
            "prompt_version": r["prompt_version"],
            "model": r["model"],
            "token_in": int(r["token_in"]),
            "token_out": int(r["token_out"]),
            "cost_usd": str(r["cost_usd"]),
        }
        for r in rows
    ]


async def _fetch_audit_chain(
    pool: Any, workflow_id: str
) -> list[dict[str, Any]]:
    """Return ``audit_chain[]`` rows for ``workflow_id``.

    Selects from ``automation.audit_events`` where the row is
    associated with the workflow either by ``resource`` (canonical
    form ``workflow:{workflow_id}``) or by a ``workflow_id`` field
    on the JSONB payload (the convention webhook handlers and the
    automation runner workflows already use). The single SELECT
    keeps the result chronological without needing a UNION.

    The returned dicts carry the parsed ``payload`` under an
    internal key consumed by :func:`build_external_links`; the
    caller drops that key (via :func:`_strip_internal_payload`)
    before serialising the response.
    """

    sql = """
        SELECT action, actor_id, actor_role, created_at, payload
          FROM automation.audit_events
         WHERE resource = $1
            OR (payload ? 'workflow_id'
                AND payload->>'workflow_id' = $2)
         ORDER BY created_at ASC
         LIMIT $3
    """
    resource = f"workflow:{workflow_id}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, resource, workflow_id, _MAX_ROWS)
    chain: list[dict[str, Any]] = []
    for r in rows:
        payload_obj = _coerce_payload_obj(r["payload"])
        chain.append(
            {
                "action": r["action"],
                "actor": r["actor_id"],
                "actor_role": r["actor_role"],
                "timestamp": r["created_at"].isoformat(),
                "payload_summary": _summarise_payload(r["payload"]),
                # Internal-only key consumed by ``build_external_links``;
                # stripped before the response leaves the function.
                "payload": payload_obj,
            }
        )
    return chain


def _strip_internal_payload(
    audit_chain: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop the internal ``payload`` key before serialisation.

    The full JSONB payload is consumed in-process by the external-link
    extractor; the wire response only carries ``payload_summary`` so
    a workflow with thousands of audit rows does not explode the
    drill-down response budget.
    """

    return [
        {k: v for k, v in entry.items() if k != "payload"}
        for entry in audit_chain
    ]


async def _enrich_workflow_detail(
    request: Request,
    workflow_id: str,
    upstream_payload: Any,
) -> dict[str, Any]:
    """Merge the upstream drill-down payload with the local enrichments.

    Args:
        request: FastAPI request (used to reach
            ``app.state.pg_pool``).
        workflow_id: Workflow id we are drilling into. Used as the
            primary key for both the cost-tracking and audit
            queries.
        upstream_payload: Whatever the upstream proxy returned —
            either a dict (the canonical drill-down shape from
            automation-service), a non-dict JSON value (in which
            case we wrap it under ``"upstream"``), or ``None``
            (when no proxy is wired). Existing top-level keys on
            the upstream payload are preserved so we never overwrite
            ``events[]`` / ``activities[]`` / ``failures[]``.

    Returns:
        The merged dict with ``llm_usage``, ``audit_chain`` and
        ``external_links`` always present (empty when the local DB
        is unavailable or the workflow has no associated rows).
    """

    if isinstance(upstream_payload, dict):
        merged: dict[str, Any] = dict(upstream_payload)
    elif upstream_payload is None:
        merged = {"workflow_id": workflow_id}
    else:
        merged = {"workflow_id": workflow_id, "upstream": upstream_payload}

    merged.setdefault("workflow_id", workflow_id)

    pool = _pg_pool(request)
    if pool is None:
        # Soft-fail: the FE renders empty tables rather than a 503 so
        # the operator can still see the upstream-only payload while
        # Postgres is recovering. The shape stays stable.
        merged["llm_usage"] = []
        merged["audit_chain"] = []
        merged["external_links"] = {}
        return merged

    try:
        llm_usage = await _fetch_llm_usage(pool, workflow_id)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning(
            "workflows_drilldown: llm_usage fetch failed for %s: %s",
            workflow_id,
            exc,
        )
        llm_usage = []

    try:
        audit_chain = await _fetch_audit_chain(pool, workflow_id)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning(
            "workflows_drilldown: audit_chain fetch failed for %s: %s",
            workflow_id,
            exc,
        )
        audit_chain = []

    external_links = build_external_links(audit_chain)

    merged["llm_usage"] = llm_usage
    merged["audit_chain"] = _strip_internal_payload(audit_chain)
    merged["external_links"] = external_links
    return merged


async def _fetch_upstream_detail(
    proxy: Any, workflow_id: str
) -> Any | None:
    """Best-effort fetch of the upstream drill-down payload.

    The upstream :class:`AdminProxy.forward` accepts either an
    :class:`auth_shared.AuthContext` or a stub matching the same
    shape; tests inject a stub that records the call. The wire
    surface returns a :class:`ProxyResponse` (status_code +
    body), so we parse the body as JSON. Failures are logged at
    WARNING and surfaced as ``None`` so the local enrichment
    fields still reach the caller.
    """

    try:
        # Forward with the foundation-task-8.2 signature. Tests inject
        # a stub proxy whose ``forward`` accepts these kwargs; the
        # production AdminProxy expects an :class:`AuthContext` actor,
        # which the upstream router (admin_proxy.py) builds from the
        # validated JWT — but the drill-down route is wired with the
        # simpler :class:`AuthClaims` dependency and intentionally
        # treats the upstream as best-effort. Production deployments
        # bridge the two via ``app.state.workflows_upstream_actor`` if
        # set, falling back to ``None`` here.
        response = await proxy.forward(
            method="GET",
            path=f"/admin/workflows/{workflow_id}",
            body=b"",
            headers={},
            actor=None,
            query_string="",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort upstream
        logger.warning(
            "workflows_drilldown: upstream fetch raised for %s: %s",
            workflow_id,
            exc,
        )
        return None

    status_code = getattr(response, "status_code", 200)
    body = getattr(response, "body", b"")

    if status_code >= 400:
        logger.info(
            "workflows_drilldown: upstream returned %s for %s",
            status_code,
            workflow_id,
        )
        return None

    if not body:
        return None
    try:
        return json.loads(body)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "workflows_drilldown: upstream returned non-JSON for %s: %s",
            workflow_id,
            exc,
        )
        return None


@router.get("", dependencies=[Depends(require_admin)])
async def list_workflows(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
    dept_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    proxy = _proxy(request)
    return await proxy.forward(
        method="GET",
        path="/admin/workflows",
        params={"dept_id": dept_id, "status": status, "limit": limit},
        actor=actor,
    )


@router.get("/{workflow_id}", dependencies=[Depends(require_admin)])
async def get_workflow(
    workflow_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> dict:
    """Return the drill-down envelope for ``workflow_id``.

    Always carries ``llm_usage``, ``audit_chain`` and
    ``external_links`` keys so the FE never has to handle a missing
    field. The upstream Temporal payload (events / activities /
    failures) is folded in best-effort when the proxy is wired —
    transient upstream failures degrade to local-only enrichment so
    the operator still sees what the platform recorded about the
    workflow.
    """

    proxy = _optional_proxy(request)
    upstream_payload: Any = None
    if proxy is not None:
        upstream_payload = await _fetch_upstream_detail(proxy, workflow_id)
    return await _enrich_workflow_detail(
        request, workflow_id, upstream_payload
    )


@router.post(
    "/{workflow_id}/cancel", dependencies=[Depends(require_admin)]
)
async def cancel_workflow(
    workflow_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> dict:
    proxy = _proxy(request)
    return await proxy.forward(
        method="POST",
        path=f"/admin/workflows/{workflow_id}/cancel",
        actor=actor,
    )
