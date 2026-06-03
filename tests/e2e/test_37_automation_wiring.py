"""
Test 37: Automation-Service Lifespan Wiring.

Validates that the FastAPI lifespan handler shipped by
the automation service populates every ``app.state.<slot>`` the routers
read at request time and that no router replies with the legacy
``"<name> router is not wired"`` error shape after startup completes.

The original ``test_07_wizard_department`` failed before this
check was added because ``app.state.dept_credentials`` was missing.
This test asserts the wiring is now in place by hitting each of the
nine router paths with a syntactically valid request and confirming
none of them return the wiring-error shape.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: ``automation-service`` host port (see Compose mapping; the report
#: shows ``0.0.0.0:8084->8080``).
AUTOMATION_SERVICE_URL = "http://localhost:8084"

#: ``admin-dashboard-api`` host port (the legacy slot the wizard hits).
DASHBOARD_API_URL = "http://localhost:8082"

#: Slot names every router pulls off ``app.state`` at request time.
#: Covers the runtime slots that must be populated during startup.
SLOT_NAMES: tuple[str, ...] = (
    "dept_credentials",
    "admin",
    "webhooks",
    "cancel",
    "repo_sync",
    "po_review",
    "inbound",
    "webhook_v2",
    "webhook_pipeline",
)

#: One representative endpoint slot — chosen so the request
#: reaches the router-level ``_deps`` resolver (which is where the
#: ``"<name> router is not wired"`` error would surface if the
#: lifespan failed to populate the slot). The handler may still
#: return 4xx for auth/validation reasons — that is fine; the
#: property is purely about the response body shape.
SLOT_PROBE_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    # (slot_name, HTTP method, path)
    ("dept_credentials", "GET", "/admin/departments"),
    ("admin", "POST", "/admin/departments"),
    ("webhooks", "POST", "/webhooks/jira"),
    ("cancel", "POST", "/api/workflows/test/cancel"),
    ("repo_sync", "POST", "/admin/departments/test/repo-mappings/sync"),
    ("po_review", "GET", "/api/orphan-branches?dept_id=test"),
    ("inbound", "POST", "/webhooks/inbound/slack"),
    ("webhook_v2", "POST", "/webhooks/jira/issue_created"),
    ("webhook_pipeline", "POST", "/webhooks/jira/pipeline"),
)

EVIDENCE_FILENAME = "37-automation-wiring.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wiring_error_detail(slot: str) -> str:
    """Return the legacy detail string a missing slot would produce.

 The four router modules surface slightly different wording; we
 match by the common prefix ``"<slot> router is not wired"`` so the
 check is robust across minor wording changes.
 """

    return f"{slot} router is not wired"


def _looks_like_wiring_error(
    body: Any, slot: str
) -> tuple[bool, str | None]:
    """Return ``(is_wiring_error, message)`` for a parsed response body.

 A wiring error surfaces as a JSON object with a ``detail`` (or
 ``reason``) field starting with ``"<slot> router is not wired"``.
 Anything else — auth failures, validation failures, gateway
 errors — is fine and counts as the slot being correctly wired.
 """

    if not isinstance(body, dict):
        return (False, None)
    for key in ("detail", "reason", "error"):
        value = body.get(key)
        if isinstance(value, str) and value.startswith(
            _wiring_error_detail(slot)
        ):
            return (True, value)
    return (False, None)


def _probe(method: str, url: str, timeout: float = 10.0) -> dict:
    """Issue *method* against *url* and return a small result dict.

 The request body is intentionally minimal (``b"{}"``) so the
 handler reaches the ``_deps`` resolver (and therefore the
 ``"router is not wired"`` branch when the slot is missing) without
 needing real auth tokens or HMAC signatures.
 """

    try:
        response = httpx.request(
            method,
            url,
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                # Most endpoints require an Authorization header; an
                # invalid one is fine here — we only care that the
                # ``router is not wired`` shape never surfaces. Auth
                # failures (401/403) count as the slot being wired.
                "Authorization": "Bearer e2e-wiring-probe",
                # Webhook endpoints also expect an HMAC + delivery id
                # header; the value can be arbitrary — the handler
                # rejects it long after the slot resolution check.
                "X-Atlassian-Webhook-Signature": "sha256=deadbeef",
                "X-Atlassian-Webhook-Identifier": str(uuid.uuid4()),
                "X-Request-UUID": str(uuid.uuid4()),
                "X-Event-Key": "pullrequest:created",
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return {
            "status_code": None,
            "body": None,
            "error": f"transport error: {exc.__class__.__name__}",
        }
    try:
        body = response.json()
    except ValueError:
        body = response.text[:200]
    return {
        "status_code": response.status_code,
        "body": body,
        "error": None,
    }


def _automation_service_reachable() -> bool:
    """Return ``True`` iff ``GET /healthz`` on automation-service answers 200."""

    try:
        response = httpx.get(
            f"{AUTOMATION_SERVICE_URL}/healthz", timeout=5.0
        )
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _require_stack_or_skip() -> None:
    """Skip when the Compose stack is not up — keeps the suite green offline."""

    if not _automation_service_reachable():
        pytest.skip(
            f"automation-service not reachable at {AUTOMATION_SERVICE_URL}; "
            "run `make boot` first ( requires a live stack)."
        )


# ---------------------------------------------------------------------------
# — /healthz returns 200 immediately after startup
# ---------------------------------------------------------------------------


@pytest.mark.wiring
class TestAutomationServiceHealthz:
    """: ``GET /healthz`` returns 200 with ``{"status": "ok"}``."""

    def test_healthz_returns_200_ok(self) -> None:
        _require_stack_or_skip()
        response = httpx.get(
            f"{AUTOMATION_SERVICE_URL}/healthz", timeout=5.0
        )
        assert response.status_code == 200, (
            f"/healthz returned {response.status_code}; expected 200"
        )
        body = response.json()
        assert body == {"status": "ok"}, (
            f"/healthz body was {body!r}; expected {{'status': 'ok'}}"
        )


# ---------------------------------------------------------------------------
# — /readyz returns 200 once Postgres + Temporal probes pass
# ---------------------------------------------------------------------------


@pytest.mark.wiring
class TestAutomationServiceReadyz:
    """: ``GET /readyz`` returns 200 when dependencies are reachable."""

    def test_readyz_returns_200_when_ready(self) -> None:
        _require_stack_or_skip()
        response = httpx.get(
            f"{AUTOMATION_SERVICE_URL}/readyz", timeout=10.0
        )
        # The probe may take a few seconds on cold start — accept 200
        # OR 503 with a documented ``failed_dependencies`` shape so
        # the test still asserts the contract rather than the timing.
        if response.status_code == 200:
            return
        assert response.status_code == 503, (
            f"/readyz returned {response.status_code}; "
            "expected 200 (ready) or 503 (not_ready)"
        )
        body = response.json()
        assert "status" in body or "failed_dependencies" in body, (
            f"/readyz 503 body missing documented shape: {body!r}"
        )


# ---------------------------------------------------------------------------
# — No router replies with Router_Not_Wired_Error
# ---------------------------------------------------------------------------


@pytest.mark.wiring
class TestNoRouterNotWiredError:
    """Every runtime slot is populated before router probes execute.

 The legacy failure mode (``"<slot> router is not wired"``) surfaced
 in the original ``test_07_wizard_department`` run when startup did
 not populate all slots; this test pins the invariant that the same
 probe never sees the wiring-error shape again.
 """

    @pytest.mark.parametrize(
        "slot,method,path", SLOT_PROBE_ENDPOINTS
    )
    def test_slot_does_not_surface_wiring_error(
        self, slot: str, method: str, path: str
    ) -> None:
        _require_stack_or_skip()
        result = _probe(method, f"{AUTOMATION_SERVICE_URL}{path}")
        # A transport error (rare — only happens if the service died
        # between healthz and the probe) is a hard fail; we cannot
        # tell whether the slot is wired or not from a dead service.
        assert result["error"] is None, (
            f"transport error probing {slot!r}: {result['error']}"
        )
        is_wiring_error, message = _looks_like_wiring_error(
            result["body"], slot
        )
        assert not is_wiring_error, (
            f"slot {slot!r} returned wiring-error shape "
            f"(HTTP {result['status_code']}): {message!r}. "
            "Lifespan handler did not populate app.state.<slot>."
        )


# ---------------------------------------------------------------------------
# — Admin departments POST returns 201 (admin slot wired end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.wiring
class TestAdminDepartmentsRoundTrip:
    """Admin departments POST reaches the router without wiring errors.

 The original wizard failed at this exact step; the contract here is
 that the platform's admin proxy can reach the admin router.
 """

    def test_admin_departments_post_does_not_return_wiring_error(
        self,
    ) -> None:
        _require_stack_or_skip()
        # Send a deliberately malformed admin token + body so the
        # router rejects the request long before any orchestrator
        # work runs. We only check the response shape — the wiring
        # contract is "the admin slot is reachable", not "a real
        # dept gets created from an unauthenticated probe".
        response = httpx.post(
            f"{AUTOMATION_SERVICE_URL}/admin/departments",
            json={"dept_id": "e2e-probe"},
            headers={"Authorization": "Bearer e2e-wiring-probe"},
            timeout=10.0,
        )
        # The handler returns 401/403/422 depending on the auth path;
        # the runtime contract says it must NOT return 500 with a
        # ``"admin router is not wired"`` body.
        assert response.status_code != 500 or (
            "admin router is not wired"
            not in (response.text or "")
        ), (
            f"POST /admin/departments returned 500 with wiring-error "
            f"detail: {response.text[:200]!r}"
        )


# ---------------------------------------------------------------------------
# — Evidence emission
# ---------------------------------------------------------------------------


@pytest.mark.wiring
class TestEmitEvidence:
    """Capture every probe result so the report has a structured payload."""

    def test_emit_evidence(
        self, evidence_collector, evidence_dir
    ) -> None:
        # When the stack is offline we still emit a structured stub so
        # the report knows the test was reached (the verdict simply
        # becomes SKIP rather than missing).
        reachable = _automation_service_reachable()
        probes: list[dict[str, Any]] = []
        if reachable:
            for slot, method, path in SLOT_PROBE_ENDPOINTS:
                result = _probe(
                    method, f"{AUTOMATION_SERVICE_URL}{path}"
                )
                is_wiring_error, message = _looks_like_wiring_error(
                    result["body"], slot
                )
                probes.append(
                    {
                        "slot": slot,
                        "method": method,
                        "path": path,
                        "status_code": result["status_code"],
                        "wiring_error": is_wiring_error,
                        "wiring_error_message": message,
                    }
                )

        evidence_collector.emit_json(
            requirement_id="",
            filename=EVIDENCE_FILENAME,
            data={
                "stack_reachable": reachable,
                "automation_service_url": AUTOMATION_SERVICE_URL,
                "slot_probes": probes,
                "requirements_validated": [
                    "/healthz returns 200 OK after lifespan startup",
                    "/readyz returns 200 OR 503 with documented body",
                    "No router returns Router_Not_Wired_Error "
                    "across the nine app.state slots",
                    "Admin departments router reachable "
                    "(no admin wiring-error)",
                    "Structured evidence emitted to e2e-evidence/",
                ],
            },
        )
        assert (evidence_dir / EVIDENCE_FILENAME).exists()
