"""
Shared pytest configuration and fixtures for the Local E2E test suite.

This conftest provides session-scoped fixtures for:
- Docker client (docker SDK)
- Credential loader (parsed from CREDENTIALS.md)
- Evidence collector (auto-creates e2e-evidence/ directory)
- Stack state tracking (which services are running)
- pytest-ordering configuration (sequential test execution)
- Playwright browser state tracking (MCP-based, not Python library)

Requirements: R1-R36 (shared infrastructure)
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Optional

import pytest

# ---------------------------------------------------------------------------
# Path setup: ensure workspace root and e2e modules are importable
# ---------------------------------------------------------------------------

# Workspace root is 3 levels up from this file:
# platform/tests/e2e/conftest.py -> platform/tests/e2e -> platform/tests -> platform -> workspace root
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PLATFORM_ROOT = WORKSPACE_ROOT / "platform"
E2E_DIR = Path(__file__).resolve().parent

# Add e2e directory to path so credential_loader, evidence_collector etc. are importable
if str(E2E_DIR) not in sys.path:
    sys.path.insert(0, str(E2E_DIR))


# ---------------------------------------------------------------------------
# pytest-ordering configuration: ensure tests run in file-number order
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    """Sort test items by their module filename to enforce execution order.

    Tests are named test_01_*, test_02_*, etc. This hook ensures they run
    in numerical order regardless of filesystem discovery order.
    """
    items.sort(key=lambda item: item.fspath.basename)


# ---------------------------------------------------------------------------
# Docker client fixture (session-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def docker_client():
    """Provide a Docker SDK client connected to the local Docker Desktop.

    Session-scoped so we reuse the same connection across all tests.
    Yields the client and closes it after the session.
    """
    import docker

    client = docker.from_env()
    # Verify Docker is reachable
    try:
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.fail(
            f"Docker Desktop is not reachable. Ensure Docker Desktop is running.\n"
            f"Error: {exc}"
        )
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Credential loader fixture (session-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def credentials():
    """Load credentials from CREDENTIALS.md at workspace root.

    Returns a Credentials dataclass with all parsed credential fields.
    Fails the session if CREDENTIALS.md is missing or malformed.
    """
    from credential_loader import load_credentials

    creds_path = WORKSPACE_ROOT / "CREDENTIALS.md"
    if not creds_path.exists():
        pytest.fail(
            f"CREDENTIALS.md not found at {creds_path}. "
            f"This file is required for E2E tests with real API credentials."
        )

    return load_credentials(creds_path)


# ---------------------------------------------------------------------------
# Evidence collector fixture (session-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def evidence_collector():
    """Provide an EvidenceCollector that writes to e2e-evidence/ at workspace root.

    Auto-creates the evidence directory on initialization.
    Session-scoped so all tests share the same collector instance.
    """
    from evidence_collector import EvidenceCollector

    collector = EvidenceCollector(base_dir=WORKSPACE_ROOT)
    yield collector
    # Generate the INDEX.md at session end
    collector.generate_index()


# ---------------------------------------------------------------------------
# Stack state tracking (session-scoped)
# ---------------------------------------------------------------------------

@dataclass
class StackState:
    """Tracks which services are currently running in the local Docker stack."""

    services: Dict[str, str] = field(default_factory=dict)
    """Map of service_name -> status (e.g., 'running', 'healthy', 'stopped')"""

    boot_time_seconds: Optional[float] = None
    """Time taken for make boot to complete."""

    full_stack_time_seconds: Optional[float] = None
    """Time from boot to full stack healthy."""

    wizard_completed: bool = False
    """Whether the Setup Wizard has been fully completed."""

    def mark_running(self, service: str, status: str = "running"):
        """Mark a service as running with given status."""
        self.services[service] = status

    def mark_stopped(self, service: str):
        """Mark a service as stopped."""
        self.services[service] = "stopped"

    def is_healthy(self, service: str) -> bool:
        """Check if a service is in healthy state."""
        return self.services.get(service) == "healthy"

    @property
    def all_healthy(self) -> bool:
        """Check if all tracked services are healthy."""
        return all(s == "healthy" for s in self.services.values()) if self.services else False


@pytest.fixture(scope="session")
def stack_state():
    """Session-scoped dict tracking which services are running and their status.

    Shared across all tests so later tests can check what earlier tests established.
    """
    return StackState()


# ---------------------------------------------------------------------------
# Playwright browser state tracking (MCP-based)
# ---------------------------------------------------------------------------

@dataclass
class PlaywrightState:
    """Tracks Playwright MCP browser state across tests.

    NOTE: This test suite uses Playwright MCP tools (not the Python library).
    The actual browser interactions happen via MCP tool calls. This fixture
    tracks the logical state of the browser session for coordination between
    test modules.
    """

    browser_launched: bool = False
    """Whether a Chromium browser has been launched via Playwright MCP."""

    current_url: Optional[str] = None
    """The current page URL (tracked logically, actual state is in MCP)."""

    har_recording_path: Optional[str] = None
    """Path where HAR recording is being saved."""

    screenshots_taken: list = field(default_factory=list)
    """List of screenshot paths taken during the session."""

    wizard_step: int = 0
    """Current wizard step (0 = not started, 1-7 = in progress/completed)."""

    def mark_launched(self, url: str, har_path: Optional[str] = None):
        """Mark browser as launched and navigated to URL."""
        self.browser_launched = True
        self.current_url = url
        self.har_recording_path = har_path

    def mark_navigated(self, url: str):
        """Update current URL after navigation."""
        self.current_url = url

    def record_screenshot(self, path: str):
        """Record that a screenshot was taken."""
        self.screenshots_taken.append(path)

    def advance_wizard(self, step: int):
        """Mark wizard progress."""
        self.wizard_step = step


@pytest.fixture(scope="session")
def playwright_state():
    """Session-scoped Playwright browser state tracker.

    Since this suite uses Playwright MCP tools (not the Python library),
    this fixture provides a shared state object that test modules use to
    coordinate browser session state (launched, current URL, wizard progress).
    """
    return PlaywrightState()


# ---------------------------------------------------------------------------
# Workspace root path fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def workspace_root() -> Path:
    """Return the workspace root path for use in tests."""
    return WORKSPACE_ROOT


@pytest.fixture(scope="session")
def platform_root() -> Path:
    """Return the platform/ directory path for use in tests."""
    return PLATFORM_ROOT


# ---------------------------------------------------------------------------
# Evidence directory fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def evidence_dir() -> Path:
    """Return the e2e-evidence/ directory path, creating it if needed."""
    edir = WORKSPACE_ROOT / "e2e-evidence"
    edir.mkdir(parents=True, exist_ok=True)
    return edir
