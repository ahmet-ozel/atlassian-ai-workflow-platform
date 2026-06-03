"""CI gate for the admin-dashboard page catalog and dynamic routes.

The admin-dashboard Next.js app MUST ship the nine pages enumerated
in design.md §"Admin Dashboard UI" — services, workflows, departments,
prompts, audit, costs, notifications, security, feature-flags. A
missing route is a build-time failure; an empty stub is a soft
failure surfaced by the size sniff.

Additionally, the workflow detail dynamic route ``app/workflows/[id]/page.tsx``
and its ``_components/`` sub-components MUST exist.

The dept credential dynamic route ``app/departments/[id]/page.tsx`` and
its modal sub-components (``CredentialModal.tsx``, ``CredentialServiceTab.tsx``)
MUST exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PAGES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "ui"
    / "admin-dashboard"
    / "app"
)

_REQUIRED_ROUTES: tuple[str, ...] = (
    "services",
    "workflows",
    "departments",
    "prompts",
    "audit",
    "costs",
    "notifications",
    "security",
    "feature-flags",
)

# Workflow detail dynamic route components.
_WORKFLOW_DETAIL_COMPONENTS: tuple[str, ...] = (
    "Header",
    "EventHistoryTimeline",
    "ActivityList",
    "LlmUsageTable",
    "AuditChain",
    "ExternalLinks",
    "CancelButton",
)


def test_admin_pages_dir_exists() -> None:
    assert _PAGES_DIR.is_dir(), (
        "Missing ui/admin-dashboard/app/ — the Next.js app router "
        "tree is required for the admin-dashboard routes."
    )


@pytest.mark.parametrize("route", _REQUIRED_ROUTES)
def test_required_admin_route_ships_a_page(route: str) -> None:
    page = _PAGES_DIR / route / "page.tsx"
    assert page.is_file(), (
        f"Missing /{route} page.tsx — the admin-dashboard must expose "
        "admin-dashboard expose this route."
    )
    body = page.read_text(encoding="utf-8")
    assert len(body) > 50, (
        f"/{route} page.tsx is too short ({len(body)} bytes); the "
        "stub must at minimum render a placeholder component."
    )


# ---------------------------------------------------------------------------
# Workflow detail route
# ---------------------------------------------------------------------------


def test_workflow_detail_dynamic_route_exists() -> None:
    """``app/workflows/[id]/page.tsx`` must exist."""
    detail_page = _PAGES_DIR / "workflows" / "[id]" / "page.tsx"
    assert detail_page.is_file(), (
        "Missing app/workflows/[id]/page.tsx — the app must expose the "
        "workflow detail dynamic route."
    )
    body = detail_page.read_text(encoding="utf-8")
    assert len(body) > 100, (
        f"app/workflows/[id]/page.tsx is too short ({len(body)} bytes); "
        "it must render the full workflow detail view."
    )


def test_workflow_detail_page_imports_components() -> None:
    """The detail page must import its sub-components."""
    detail_page = _PAGES_DIR / "workflows" / "[id]" / "page.tsx"
    assert detail_page.is_file(), "app/workflows/[id]/page.tsx missing"
    body = detail_page.read_text(encoding="utf-8")
    for component in _WORKFLOW_DETAIL_COMPONENTS:
        assert component in body, (
            f"app/workflows/[id]/page.tsx does not import/reference "
            f"'{component}' — the page must reference all sub-components."
        )


@pytest.mark.parametrize("component", _WORKFLOW_DETAIL_COMPONENTS)
def test_workflow_detail_component_exists(component: str) -> None:
    """Each ``_components/`` file must exist and be non-trivial."""
    comp_file = _PAGES_DIR / "workflows" / "[id]" / "_components" / f"{component}.tsx"
    assert comp_file.is_file(), (
        f"Missing app/workflows/[id]/_components/{component}.tsx — "
        "all workflow detail sub-components must be present."
    )
    body = comp_file.read_text(encoding="utf-8")
    assert len(body) > 50, (
        f"_components/{component}.tsx is too short ({len(body)} bytes); "
        "the component must contain a real implementation."
    )


def test_workflow_detail_page_has_cancel_button() -> None:
    """The detail page must include a CancelButton."""
    detail_page = _PAGES_DIR / "workflows" / "[id]" / "page.tsx"
    assert detail_page.is_file(), "app/workflows/[id]/page.tsx missing"
    body = detail_page.read_text(encoding="utf-8")
    assert "CancelButton" in body, (
        "app/workflows/[id]/page.tsx must include CancelButton — "
        "the detail view needs a RBAC-aware cancel action."
    )


def test_workflow_detail_page_fetches_admin_workflows_endpoint() -> None:
    """The detail page must call ``/admin/workflows/``."""
    detail_page = _PAGES_DIR / "workflows" / "[id]" / "page.tsx"
    assert detail_page.is_file(), "app/workflows/[id]/page.tsx missing"
    body = detail_page.read_text(encoding="utf-8")
    assert "/admin/workflows/" in body, (
        "app/workflows/[id]/page.tsx must fetch from /admin/workflows/{id} — "
        "the drilldown endpoint must be consumed."
    )


def test_workflows_list_page_links_to_detail_route() -> None:
    """The workflows list page must link to ``/workflows/{id}``."""
    list_page = _PAGES_DIR / "workflows" / "page.tsx"
    assert list_page.is_file(), "app/workflows/page.tsx missing"
    body = list_page.read_text(encoding="utf-8")
    assert "/workflows/" in body, (
        "app/workflows/page.tsx must contain links to /workflows/{id} — "
        "the list page must link to the detail route."
    )


def test_admin_shell_links_streamlit_debug_tools() -> None:
    """Streamlit ops/debug tools must be linked from admin navigation.

 Governance surfaces (Workflows, PO Review, Orphan Branches) now
 live as native admin-dashboard routes, not Streamlit links. Only
 the read-only MCP debug tools remain external Streamlit links.
 """

    shell = _PAGES_DIR.parent / "components" / "AppShell.tsx"
    body = shell.read_text(encoding="utf-8")
    assert "MCP Explorer" in body
    assert "MCP Inspector" in body
    assert "/explorer" in body
    assert "/mcp_inspector" in body
    assert "NEXT_PUBLIC_STREAMLIT_URL" in body


def test_admin_shell_links_po_review_route() -> None:
    """PO Review is a native admin-dashboard route (moved from Streamlit)."""

    shell = _PAGES_DIR.parent / "components" / "AppShell.tsx"
    body = shell.read_text(encoding="utf-8")
    assert "/po-review" in body, (
        "AppShell must link the native /po-review route; PO review moved "
        "out of Streamlit into the admin dashboard."
    )
    po_page = _PAGES_DIR / "po-review" / "page.tsx"
    assert po_page.is_file(), "app/po-review/page.tsx missing"


# ---------------------------------------------------------------------------
# Dept credential UI route
# ---------------------------------------------------------------------------

# Modal sub-components required by the dept credential UI.
_DEPT_CREDENTIAL_COMPONENTS: tuple[str, ...] = (
    "CredentialModal",
    "CredentialServiceTab",
)


def test_dept_credential_dynamic_route_exists() -> None:
    """``app/departments/[id]/page.tsx`` must exist."""
    detail_page = _PAGES_DIR / "departments" / "[id]" / "page.tsx"
    assert detail_page.is_file(), (
        "Missing app/departments/[id]/page.tsx — the app must provide "
        "the dept credential modal route."
    )
    body = detail_page.read_text(encoding="utf-8")
    assert len(body) > 100, (
        f"app/departments/[id]/page.tsx is too short ({len(body)} bytes); "
        "it must render the dept credential modal view."
    )


@pytest.mark.parametrize("component", _DEPT_CREDENTIAL_COMPONENTS)
def test_dept_credential_component_exists(component: str) -> None:
    """Each ``departments/_components/`` modal file must exist."""
    comp_file = (
        _PAGES_DIR / "departments" / "_components" / f"{component}.tsx"
    )
    assert comp_file.is_file(), (
        f"Missing app/departments/_components/{component}.tsx — "
        "the dept credential modal sub-components must be present."
    )
    body = comp_file.read_text(encoding="utf-8")
    assert len(body) > 50, (
        f"_components/{component}.tsx is too short ({len(body)} bytes); "
        "the component must contain a real implementation."
    )
