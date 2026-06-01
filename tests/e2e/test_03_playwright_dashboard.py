"""
Test 03: Playwright browser launch and admin-dashboard access.

These checks drive a real Chromium browser through pytest-playwright.
They validate that the admin dashboard is reachable, renders without a
Next.js runtime error, exposes the setup wizard with seven steps, and
emits browser evidence (HAR + screenshots) for the E2E report.

Requirements: R3.1, R3.2, R3.3, R3.4, R3.5
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect


DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:3000").rstrip("/")
PAGE_LOAD_TIMEOUT_MS = 45_000
PAGE_LOAD_TIMEOUT_SECONDS = PAGE_LOAD_TIMEOUT_MS / 1000
WIZARD_VISIBLE_TIMEOUT_MS = 15_000
EXPECTED_WIZARD_STEPS = 7
HAR_FILENAME = "03-dashboard.har"
SCREENSHOT_FILENAME = "03-dashboard-initial.png"

CORE_ADMIN_ROUTES = [
    "/services",
    "/departments",
    "/operations",
    "/prompts",
    "/llm-providers",
]

RUNTIME_ERROR_MARKERS = [
    "Application error",
    "Unhandled Runtime Error",
    "Internal Server Error",
    "This page could not be found",
]


def _url(path: str = "") -> str:
    if not path:
        return DASHBOARD_URL
    return f"{DASHBOARD_URL}{path if path.startswith('/') else '/' + path}"


def _check_dashboard_accessible(
    url: str = DASHBOARD_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    start = time.time()
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        elapsed = time.time() - start
        return {
            "accessible": response.status_code == 200,
            "status_code": response.status_code,
            "latency_ms": round(elapsed * 1000, 2),
            "content_length": len(response.text),
            "error": None,
        }
    except Exception as exc:  # pragma: no cover - exercised only on live outage
        elapsed = time.time() - start
        return {
            "accessible": False,
            "status_code": None,
            "latency_ms": round(elapsed * 1000, 2),
            "content_length": 0,
            "error": str(exc),
        }


def _failure_text(request: Any) -> str:
    failure = getattr(request, "failure", None)
    if callable(failure):
        failure = failure()
    return str(failure or "")


def _attach_browser_diagnostics(page: Page) -> dict[str, list[Any]]:
    diagnostics: dict[str, list[Any]] = {
        "console_errors": [],
        "request_failures": [],
    }

    page.on(
        "console",
        lambda msg: diagnostics["console_errors"].append(msg.text)
        if msg.type == "error"
        else None,
    )
    page.on(
        "requestfailed",
        lambda request: diagnostics["request_failures"].append(
            {
                "method": request.method,
                "url": request.url,
                "failure": _failure_text(request),
            }
        ),
    )
    return diagnostics


def _goto_admin_page(page: Page, path: str = ""):
    response = page.goto(
        _url(path),
        wait_until="domcontentloaded",
        timeout=PAGE_LOAD_TIMEOUT_MS,
    )
    assert response is not None, "Playwright did not receive a navigation response"
    assert 200 <= response.status < 400, (
        f"{_url(path)} returned HTTP {response.status}; "
        f"body={_body_text(page)[:300]!r}"
    )
    expect(page.locator("body")).to_be_visible(timeout=5_000)
    return response


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


def _assert_no_runtime_error(page: Page) -> None:
    body_text = _body_text(page)
    for marker in RUNTIME_ERROR_MARKERS:
        assert marker not in body_text, (
            f"Dashboard rendered a runtime error marker {marker!r}; "
            f"body={body_text[:500]!r}"
        )


def _capture_page_screenshot(
    page: Page,
    evidence_collector,
    filename: str,
    requirement_id: str,
) -> Path:
    screenshot_bytes = page.screenshot(full_page=True)
    return evidence_collector.save_screenshot(
        requirement_id=requirement_id,
        filename=filename,
        screenshot_bytes=screenshot_bytes,
    )


@pytest.mark.wizard
class TestPlaywrightBrowserLaunch:
    """R3.1: launch Chromium and navigate to the dashboard."""

    def test_dashboard_is_accessible(self) -> None:
        result = _check_dashboard_accessible(
            DASHBOARD_URL,
            timeout=PAGE_LOAD_TIMEOUT_SECONDS,
        )
        assert result["accessible"], (
            f"Dashboard at {DASHBOARD_URL} is not accessible. "
            f"Status={result['status_code']} error={result['error']!r}. "
            "Start admin-dashboard-ui before running browser E2E tests."
        )

    def test_browser_navigate_to_dashboard(
        self,
        browser: Browser,
        playwright_state,
        evidence_dir: Path,
    ) -> None:
        har_path = evidence_dir / HAR_FILENAME
        context = browser.new_context(record_har_path=str(har_path))
        page = context.new_page()
        diagnostics = _attach_browser_diagnostics(page)

        try:
            response = _goto_admin_page(page)
            _assert_no_runtime_error(page)
            playwright_state.mark_launched(
                url=page.url,
                har_path=str(har_path),
            )
            assert response.status == 200
        finally:
            context.close()

        assert playwright_state.browser_launched is True
        assert playwright_state.current_url.startswith(DASHBOARD_URL)
        assert har_path.exists(), f"HAR evidence was not created at {har_path}"
        assert isinstance(diagnostics["console_errors"], list)


@pytest.mark.wizard
class TestDashboardPageContent:
    """R3.2/R3.5: validate the rendered dashboard DOM."""

    def test_page_title_contains_expected_text(self, page: Page) -> None:
        _goto_admin_page(page)
        expect(page).to_have_title(re.compile(r"(Admin|Dashboard)", re.I))
        _assert_no_runtime_error(page)

    def test_setup_wizard_visible(self, page: Page, evidence_collector) -> None:
        _goto_admin_page(page)
        step_items = page.locator("ol.steps > li.step")
        try:
            expect(step_items).to_have_count(
                EXPECTED_WIZARD_STEPS,
                timeout=WIZARD_VISIBLE_TIMEOUT_MS,
            )
        except AssertionError as exc:
            _capture_page_screenshot(
                page,
                evidence_collector,
                "03-dashboard-wizard-missing.png",
                "R3.2,R3.5",
            )
            raise AssertionError(
                "Setup wizard did not render seven steps. "
                "If the backend setup API is unavailable, the page will show "
                "the connection-error state and this E2E test should fail."
            ) from exc

        first_class = step_items.nth(0).get_attribute("class") or ""
        assert "is-done" not in first_class
        assert "is-failed" not in first_class
        _assert_no_runtime_error(page)


@pytest.mark.wizard
class TestDashboardRoutes:
    """Browser smoke checks for core admin-dashboard routes."""

    @pytest.mark.parametrize("path", CORE_ADMIN_ROUTES)
    def test_core_admin_route_renders_in_browser(self, page: Page, path: str) -> None:
        _goto_admin_page(page, path)
        _assert_no_runtime_error(page)


@pytest.mark.wizard
class TestDashboardScreenshot:
    """R3.3: capture a real browser screenshot as evidence."""

    def test_capture_dashboard_screenshot(
        self,
        page: Page,
        playwright_state,
        evidence_collector,
    ) -> None:
        _goto_admin_page(page)
        screenshot_path = _capture_page_screenshot(
            page,
            evidence_collector,
            SCREENSHOT_FILENAME,
            "R3.3",
        )
        playwright_state.record_screenshot(str(screenshot_path))
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 100


@pytest.mark.wizard
class TestDashboardLoadFailure:
    """R3.4: emit actionable evidence if the browser cannot load the page."""

    def test_dashboard_load_timeout_handling(
        self,
        page: Page,
        evidence_collector,
    ) -> None:
        diagnostics = _attach_browser_diagnostics(page)
        try:
            _goto_admin_page(page)
            _assert_no_runtime_error(page)
        except Exception as exc:
            screenshot_path = _capture_page_screenshot(
                page,
                evidence_collector,
                "03-dashboard-error.png",
                "R3.4",
            )
            evidence_collector.emit_json(
                requirement_id="R3.4",
                filename="03-dashboard-error.json",
                data={
                    "url": DASHBOARD_URL,
                    "error": str(exc),
                    "console_errors": diagnostics["console_errors"],
                    "request_failures": diagnostics["request_failures"],
                    "screenshot": str(screenshot_path),
                    "verdict": "fail",
                },
            )
            raise


@pytest.mark.wizard
class TestDashboardEvidence:
    """Emit consolidated browser evidence for R3."""

    def test_emit_dashboard_evidence(
        self,
        page: Page,
        playwright_state,
        evidence_collector,
        evidence_dir: Path,
    ) -> None:
        diagnostics = _attach_browser_diagnostics(page)
        response = _goto_admin_page(page)
        title = page.title()
        step_items = page.locator("ol.steps > li.step")
        try:
            expect(step_items).to_have_count(
                EXPECTED_WIZARD_STEPS,
                timeout=WIZARD_VISIBLE_TIMEOUT_MS,
            )
        except AssertionError:
            # Keep evidence emission useful even when the page is in an API
            # error state; the assertion lives in test_setup_wizard_visible.
            pass
        steps_count = step_items.count()
        screenshot_path = _capture_page_screenshot(
            page,
            evidence_collector,
            "03-dashboard-evidence.png",
            "R3.1,R3.2,R3.3,R3.4,R3.5",
        )

        evidence_collector.emit_json(
            requirement_id="R3.1,R3.2,R3.3,R3.4,R3.5",
            filename="03-dashboard.json",
            data={
                "url": DASHBOARD_URL,
                "http_status": response.status,
                "title": title,
                "title_valid": bool(re.search(r"(Admin|Dashboard)", title, re.I)),
                "wizard_steps_count": steps_count,
                "expected_wizard_steps": EXPECTED_WIZARD_STEPS,
                "console_errors": diagnostics["console_errors"],
                "request_failures": diagnostics["request_failures"],
                "playwright_state": {
                    "browser_launched": playwright_state.browser_launched,
                    "current_url": playwright_state.current_url,
                    "har_recording_path": playwright_state.har_recording_path,
                    "screenshots_taken": playwright_state.screenshots_taken,
                },
                "har_evidence_path": str(evidence_dir / HAR_FILENAME),
                "screenshot_evidence_path": str(screenshot_path),
                "verdict": "pass"
                if steps_count == EXPECTED_WIZARD_STEPS
                else "needs_backend_state",
            },
        )

        evidence_path = evidence_dir / "03-dashboard.json"
        assert evidence_path.exists(), f"Evidence file not created at {evidence_path}"
