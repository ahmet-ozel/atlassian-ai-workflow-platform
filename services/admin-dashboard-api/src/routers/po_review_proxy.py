"""``po_review_proxy`` - admin-dashboard-api  automation-service shim.

The PO Review Inbox and Orphan Branches surfaces moved from the
Streamlit end-user app into the admin dashboard (admin-only). The
authoritative endpoints live on automation-service:

* ``GET  /api/po-review-inbox?dept_id=<id>``
* ``GET  /api/orphan-branches?dept_id=<id>``
* ``POST /api/po-review-inbox/{pr_id}/open-draft``
* ``POST /api/po-review-inbox/{pr_id}/request-changes``
* ``POST /api/po-review-inbox/{pr_id}/approve-note``

This router exposes the same paths under the admin-dashboard-api so
the dashboard front-end (which only talks to admin-dashboard-api on
:8082) can reach them. Every call is gated by :func:`require_admin`
- unlike the old Streamlit page, which any session could open. The
forward uses the shared ``app.state.http_client`` and the
``automation_service_url`` setting.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ..auth.dependencies import require_admin

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["po-review"], dependencies=[Depends(require_admin)])


def _upstream(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    base = str(
        getattr(settings, "automation_service_url", "")
        or "http://automation-service:8080"
    ).rstrip("/")
    return base


def _client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="http client is not wired")
    return client


async def _forward_get(request: Request, path: str, dept_id: str) -> Any:
    url = f"{_upstream(request)}{path}"
    try:
        resp = await _client(request).get(
            url,
            params={"dept_id": dept_id},
            headers=_forward_headers(request),
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    return resp.json()


async def _forward_post(
    request: Request, path: str, dept_id: str, body: dict[str, Any]
) -> Any:
    url = f"{_upstream(request)}{path}"
    try:
        resp = await _client(request).post(
            url,
            params={"dept_id": dept_id},
            json=body,
            headers=_forward_headers(request),
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    if resp.content:
        try:
            return resp.json()
        except ValueError:
            return {"status": resp.status_code}
    return {"status": resp.status_code}


def _forward_headers(request: Request) -> dict[str, str]:
    """Build headers for the upstream automation-service call.

    The dashboard's own caller has already passed :func:`require_admin`
    on this router, so the actor is a trusted admin. automation-service
    re-authorises the ``/api/po-review-inbox`` surface against its own
    OIDC validator; in dev mode any non-empty bearer is accepted, and
    in production the value is the service-to-service token configured
    via ``AUTOMATION_SERVICE_TOKEN``. We forward the caller's own
    bearer when present so the actor identity propagates for audit.
    """

    import os

    incoming = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )
    token = (
        incoming
        or f"Bearer {os.environ.get('AUTOMATION_SERVICE_TOKEN', 'dev-admin-token')}"
    )
    return {"Authorization": token}


@router.get("/po-review-inbox")
async def list_po_review_inbox(
    request: Request, dept_id: str = Query(..., min_length=1)
) -> Any:
    """List the dept's bot-authored draft PRs pending PO review."""
    return await _forward_get(request, "/api/po-review-inbox", dept_id)


@router.get("/orphan-branches")
async def list_orphan_branches(
    request: Request, dept_id: str = Query(..., min_length=1)
) -> Any:
    """List the dept's orphan ai/* branches with no open PR."""
    return await _forward_get(request, "/api/orphan-branches", dept_id)


@router.post("/po-review-inbox/{pr_id}/open-draft")
async def po_open_draft(
    request: Request,
    pr_id: int,
    dept_id: str = Query(..., min_length=1),
) -> Any:
    return await _forward_post(
        request, f"/api/po-review-inbox/{pr_id}/open-draft", dept_id, {}
    )


@router.post("/po-review-inbox/{pr_id}/request-changes")
async def po_request_changes(
    request: Request,
    pr_id: int,
    dept_id: str = Query(..., min_length=1),
    body: dict[str, Any] = Body(default_factory=dict),
) -> Any:
    return await _forward_post(
        request, f"/api/po-review-inbox/{pr_id}/request-changes", dept_id, body
    )


@router.post("/po-review-inbox/{pr_id}/approve-note")
async def po_approve_note(
    request: Request,
    pr_id: int,
    dept_id: str = Query(..., min_length=1),
    body: dict[str, Any] = Body(default_factory=dict),
) -> Any:
    return await _forward_post(
        request, f"/api/po-review-inbox/{pr_id}/approve-note", dept_id, body
    )
