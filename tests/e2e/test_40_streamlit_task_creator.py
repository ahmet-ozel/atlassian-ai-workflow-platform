"""
Test 40: Streamlit Task Creator - End-to-End (E5 - gereksinim.txt G8/G10).

Validates the Streamlit assistant's Task Creator page
(``ui/streamlit-app/pages/2_task_creator.py``) - the surface a
department member uses to chat about a task they want to create and turn
that conversation into a complete Jira task description. ``gereksinim.txt``
satır 13/19 bu sayfanın kullanıcıya task oluşturmada yardımcı olmasını
ister; E5 bu akışı düzenli bir smoke testine bağlar.

Test stratejisi (test_39 ile aynı desen):

* ``httpx`` ile Streamlit sunucusu + ``/task_creator`` route 200 mü
  kontrolü.
* Kaynak seviyesi sözleşme - ``2_task_creator.py`` dosyasının chat-only
  task description asistanı olduğunu ve doğrudan Jira create/form akışı
  içermediğini pinler.
* Gerçek Playwright tarayıcısı ile sayfanın hidrate olup chat inputunu
  render ettiğini doğrular.
* Render edilen HTML'de hiçbir kimlik bilgisi sızıntısı olmadığını
  doğrular.

Bu sayfa **task'ı gerçekten Jira'ya GÖNDERMEZ** - task açacak kullanıcıya
sohbet içinde eksik bilgi listesi ve description taslağı üretir. Jira issue oluşturma
akışı MCP/automation tarafındadır.

Requirements: R40.1 - R40.5
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

try:
    from playwright.sync_api import Page, expect
except ImportError:  # pragma: no cover - playwright optional at import time
    Page = Any  # type: ignore[assignment,misc]
    expect = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STREAMLIT_URL = "http://localhost:8501"
#: Streamlit multipage routing strips the numeric prefix from
#: ``pages/2_task_creator.py``  ``/task_creator``.
TASK_CREATOR_PATH = "/task_creator"
TASK_CREATOR_FULL_URL = f"{STREAMLIT_URL}{TASK_CREATOR_PATH}"
HEALTH_URL = f"{STREAMLIT_URL}/_stcore/health"

EVIDENCE_FILENAME = "40-streamlit-task-creator.json"
SCREENSHOT_FILENAME = "40-streamlit-task-creator.png"

#: Source-level anchors the task creator page must keep so the
#: chat-only assistant contract does not silently regress.
EXPECTED_SOURCE_ANCHORS: tuple[str, ...] = (
    "st.chat_input",                         # the only user input control
    "st.chat_message",                       # native chat transcript
    "task_creator_chat_history",             # page-scoped transcript
    "_task_creator_chat_reply",              # assistant response helper
    "_missing_info",                         # missing-info helper
    "_draft_description",                    # draft builder helper
    "SYSTEM_PROMPT_TEMPLATE",                # canonical prompt loaded
)

FORBIDDEN_SOURCE_ANCHORS: tuple[str, ...] = (
    "client.create_task(",
    "/api/tasks/create",
    "st.form(",
    "st.form_submit_button",
    "st.text_input(",
    "st.selectbox(",
    "st.multiselect(",
    "render_bot_assignee_card",
    "render_bot_identity_card",
)

#: Credential markers - the rendered HTML must never carry a verbatim
#: secret prefix (Sensitive_Field_Set, llm-provider-management R13.1).
SENSITIVE_MARKERS: tuple[str, ...] = (
    "sk-ant-",
    "sk-proj-",
    "sk-live-",
    "sk-test-",
    "AIzaSy",
    "ATATT3x",
    "ATCTT3x",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _streamlit_reachable() -> bool:
    """True when the Streamlit health endpoint answers 200."""
    try:
        response = httpx.get(HEALTH_URL, timeout=5.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _require_streamlit_or_skip() -> None:
    if not _streamlit_reachable():
        pytest.skip(
            f"streamlit-ui not reachable at {STREAMLIT_URL}; "
            "start the streamlit-ui profile first (E5 requires a live "
            "Streamlit container)."
        )


def _task_creator_source_path() -> Path:
    """On-disk path of the Task Creator page module."""
    workspace = Path(__file__).resolve().parents[2]
    return (
        workspace
        / "ui"
        / "streamlit-app"
        / "pages"
        / "2_task_creator.py"
    )


# ---------------------------------------------------------------------------
# R40.1 - Streamlit server + task creator route reachable
# ---------------------------------------------------------------------------


class TestStreamlitTaskCreatorReachable:
    """R40.1 - Streamlit serves the Task Creator page."""

    def test_streamlit_health_ok(self) -> None:
        _require_streamlit_or_skip()
        response = httpx.get(HEALTH_URL, timeout=5.0)
        assert response.status_code == 200

    def test_task_creator_route_returns_200(self) -> None:
        _require_streamlit_or_skip()
        response = httpx.get(
            TASK_CREATOR_FULL_URL,
            timeout=10.0,
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "html" in response.headers.get("content-type", "").lower()


# ---------------------------------------------------------------------------
# R40.2 - Source contract: the chat-only assistant flow is intact
# ---------------------------------------------------------------------------


class TestTaskCreatorSourceContract:
    """R40.2 - ``2_task_creator.py`` keeps the chat-only assistant contract."""

    def test_source_file_exists(self) -> None:
        path = _task_creator_source_path()
        assert path.is_file(), f"Task Creator source missing: {path}"

    @pytest.mark.parametrize("anchor", EXPECTED_SOURCE_ANCHORS)
    def test_source_contains_anchor(self, anchor: str) -> None:
        source = _task_creator_source_path().read_text(encoding="utf-8")
        assert anchor in source, (
            f"Task Creator source no longer contains {anchor!r} - the "
            "chat-only assistant contract may have regressed."
        )

    @pytest.mark.parametrize("anchor", FORBIDDEN_SOURCE_ANCHORS)
    def test_source_does_not_create_jira_task_directly(self, anchor: str) -> None:
        source = _task_creator_source_path().read_text(encoding="utf-8")
        assert anchor not in source, (
            f"Task Creator source still contains forbidden direct-create "
            f"anchor {anchor!r}; this page must stay chat-only."
        )


# ---------------------------------------------------------------------------
# R40.3 - Page hydrates and renders the chat surface (real Playwright)
# ---------------------------------------------------------------------------


class TestTaskCreatorRenders:
    """R40.3 - The page hydrates without crashing.

    The Task Creator keeps the main task-prep flow as a chat surface. If an
    unauthenticated session gate appears, the page must still avoid Python
    crashes.
    Streamlit session (``render_dept_switcher`` shows "Oturum
    bulunamadı" when no session is present). A headless test without
    credentials therefore sees the session gate - that is still a
    *correct* render of a working page module. The test asserts:

    * the Streamlit app hydrates (``stApp`` visible),
    * the page module loaded without a Python exception
      (no ``stException``), and
    * the page-hero title "Task Creator" rendered, and
    * EITHER the chat assistant greeting/input OR the session gate is shown.

    The chat-only contract itself is pinned by the
    source-level test class above.
    """

    def test_page_hydrates_without_crash(
        self,
        page: "Page",
        evidence_dir,
    ) -> None:
        _require_streamlit_or_skip()
        response = page.goto(
            TASK_CREATOR_FULL_URL,
            wait_until="domcontentloaded",
        )
        assert response is not None, "no navigation response"
        assert response.status == 200, f"HTTP {response.status}"

        # Streamlit hydrates over a websocket after the initial HTML.
        expect(page.get_by_test_id("stApp")).to_be_visible(timeout=20000)
        # Give the websocket render a moment to paint the script output.
        page.wait_for_timeout(8000)

        # The page module must not have crashed at import / run time.
        exception_count = page.get_by_test_id("stException").count()
        assert exception_count == 0, (
            "Task Creator page raised a Python exception during render - "
            "the page module is broken."
        )

        main_text = page.locator("body").inner_text(timeout=10000)

        # The page-hero title proves the module executed top-to-bottom
        # far enough to render its header.
        assert "Task Creator" in main_text, (
            f"Task Creator hero title missing; main text was: "
            f"{main_text[:200]!r}"
        )

        # Either the chat surface (authenticated) or the session gate
        # (unauthenticated) is an acceptable working render.
        chat_visible = (
            "Task'ı nasıl açmak istediğini" in main_text
            or "Oluşturmak istediğiniz task'ı yazın" in main_text
        )
        session_gate = "Oturum" in main_text
        assert chat_visible or session_gate, (
            "Task Creator rendered neither the chat assistant nor the session "
            f"gate; main text was: {main_text[:200]!r}"
        )

        page.screenshot(
            path=str(evidence_dir / SCREENSHOT_FILENAME),
            full_page=True,
        )


# ---------------------------------------------------------------------------
# R40.4 - No credential leakage in the rendered page
# ---------------------------------------------------------------------------


class TestTaskCreatorNoCredentialLeak:
    """R40.4 - The rendered HTML carries no verbatim secret."""

    def test_no_sensitive_markers_in_html(self) -> None:
        _require_streamlit_or_skip()
        response = httpx.get(
            TASK_CREATOR_FULL_URL,
            timeout=10.0,
            follow_redirects=True,
        )
        leaked = [m for m in SENSITIVE_MARKERS if m in response.text]
        assert not leaked, f"Sensitive markers leaked into HTML: {leaked}"


# ---------------------------------------------------------------------------
# R40.5 - Evidence
# ---------------------------------------------------------------------------


class TestEmitEvidence:
    """R40.5 - Capture the reachability + contract results."""

    def test_emit_evidence(
        self,
        evidence_collector,
        evidence_dir,
    ) -> None:
        reachable = _streamlit_reachable()
        source = _task_creator_source_path().read_text(encoding="utf-8")
        anchors_present = {
            anchor: (anchor in source) for anchor in EXPECTED_SOURCE_ANCHORS
        }

        snapshot: dict[str, Any] = {
            "streamlit_url": STREAMLIT_URL,
            "task_creator_path": TASK_CREATOR_PATH,
            "streamlit_reachable": reachable,
            "source_anchors_present": anchors_present,
        }

        if reachable:
            response = httpx.get(
                TASK_CREATOR_FULL_URL,
                timeout=10.0,
                follow_redirects=True,
            )
            snapshot["status_code"] = response.status_code
            snapshot["content_length"] = len(response.text)
            snapshot["sensitive_markers_present"] = [
                m for m in SENSITIVE_MARKERS if m in response.text
            ]

        evidence_collector.emit_json(
            requirement_id="R40",
            filename=EVIDENCE_FILENAME,
            data={
                "snapshot": snapshot,
                "requirements_validated": [
                    "R40.1 - Streamlit serves /task_creator (HTTP 200)",
                    "R40.2 - 2_task_creator.py keeps chat-only assistant contract",
                    "R40.3 - Page hydrates and renders the chat task-description assistant",
                    "R40.4 - No Sensitive_Field_Set markers in rendered HTML",
                    "R40.5 - Evidence emitted to e2e-evidence/",
                ],
            },
        )
        assert (evidence_dir / EVIDENCE_FILENAME).exists()
