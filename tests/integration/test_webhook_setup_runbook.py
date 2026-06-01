"""Webhook setup runbook existence and section header checks.

Validates: Requirements 3.10 (`platform-mimari-workflows`).

This test is the *basic* existence + section header check companion to
task 4.8. The full integration coverage (audit assertions, Vault path
parity, etc.) is the responsibility of task 15.11; this file only
asserts that the runbook exists and contains every section header
required by Property 19's `test_webhook_setup_runbook` pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# This is a doc-presence check, not a real Temporal integration test, but it
# is colocated under ``tests/integration/`` because it asserts a deliverable
# of the platform-mimari-workflows spec (Requirement 3.10) and runs in the
# integration lane alongside the webhook gateway tests. No external services
# (Temporal, Postgres, Vault) are touched — the test only reads the runbook
# file from disk.
pytestmark = pytest.mark.integration

# Repository-root anchor: this file lives under
# `platform/tests/integration/`; the runbook lives under
# `platform/docs/runbooks/`.
PLATFORM_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_PATH = PLATFORM_ROOT / "docs" / "runbooks" / "webhook-setup.md"


# Event types the gateway is required to support per R3.2 / R3.3, that the
# runbook must instruct admins to subscribe to in the provider UI. These are
# the same strings the production handler in
# ``automation_service/api/webhooks.py`` matches against.
REQUIRED_JIRA_EVENT_TYPES: tuple[str, ...] = (
    "jira:issue_created",
    "jira:issue_assigned",
    "jira:issue_updated",
    "jira:issue_commented",
)

REQUIRED_BITBUCKET_EVENT_TYPES: tuple[str, ...] = (
    "pullrequest:created",
    "pullrequest:commented",
    "pullrequest:updated",
)


REQUIRED_SECTION_HEADERS: tuple[str, ...] = (
    "## 1. Overview",
    "## 2. Prerequisites",
    "## 3. Jira webhook setup",
    "## 4. Bitbucket webhook setup",
    "## 5. Secret rotation",
    "## 6. Verification",
    "## 7. Troubleshooting",
    "## 8. Loop guard caveats",
)

REQUIRED_VAULT_PATHS: tuple[str, ...] = (
    "vault:webhooks/jira/{dept_id}",
    "vault:webhooks/bitbucket/{dept_id}",
)


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK_PATH.exists(), (
        f"webhook setup runbook missing at {RUNBOOK_PATH}; "
        "task 4.8 of platform-mimari-workflows must produce this file"
    )
    return RUNBOOK_PATH.read_text(encoding="utf-8")


def test_webhook_setup_runbook_file_exists() -> None:
    """The runbook file SHALL exist at platform/docs/runbooks/webhook-setup.md."""
    assert RUNBOOK_PATH.is_file(), f"expected runbook at {RUNBOOK_PATH}"


@pytest.mark.parametrize("header", REQUIRED_SECTION_HEADERS)
def test_webhook_setup_runbook_contains_required_section_headers(
    runbook_text: str, header: str
) -> None:
    """Each required section header SHALL appear verbatim in the runbook."""
    assert header in runbook_text, (
        f"required section header {header!r} not found in webhook setup runbook"
    )


@pytest.mark.parametrize("vault_path", REQUIRED_VAULT_PATHS)
def test_webhook_setup_runbook_mentions_vault_secret_paths(
    runbook_text: str, vault_path: str
) -> None:
    """The runbook SHALL document per-dept vault secret paths (R3.10 / V3)."""
    assert vault_path in runbook_text, (
        f"vault secret path {vault_path!r} not documented in webhook setup runbook; "
        "this is required by Requirement 3.10 (MIMARI §16.14.3 V3 — dept başına ayrı secret)"
    )


def test_webhook_setup_runbook_mentions_webhook_urls(runbook_text: str) -> None:
    """The runbook SHALL document the gateway URLs for both providers."""
    assert "{public_url}/webhooks/jira" in runbook_text, (
        "Jira webhook URL pattern missing"
    )
    assert "{public_url}/webhooks/bitbucket" in runbook_text, (
        "Bitbucket webhook URL pattern missing"
    )


def test_webhook_setup_runbook_mentions_signature_headers(runbook_text: str) -> None:
    """The runbook SHALL name the per-provider signature headers."""
    assert "X-Atlassian-Webhook-Signature" in runbook_text, (
        "Jira signature header not documented"
    )
    assert "X-Hub-Signature" in runbook_text, (
        "Bitbucket signature header not documented"
    )


def test_webhook_setup_runbook_documents_loop_guard_caveats(runbook_text: str) -> None:
    """The runbook SHALL document the loop guard caveats (R4.1, R4.2; N15).

    Two facts must be reachable to operators:

    - Bot ``account_id`` values must be present in ``departments.json`` so
      that the primary loop guard short-circuits bot self-actions.
    - For legacy installs that do not send ``actor.account_id``, the
      gateway falls back to a comment-body regex (``^\\s*\\[bot:``).
    """
    assert "departments.json" in runbook_text, (
        "loop guard caveat about departments.json bot account_ids missing"
    )
    assert "account_id" in runbook_text, (
        "loop guard caveat must reference bot account_id field"
    )
    assert "^\\s*\\[bot:" in runbook_text, (
        "loop guard regex fallback (^\\s*\\[bot:) for legacy installs not documented"
    )


def test_runbook_contains_jira_section_headers(runbook_text: str) -> None:
    """The runbook SHALL contain Jira-section markers required by R3.10.

    R3.10 enumerates the fields an operator must fill in the Jira UI: URL,
    Events, and Secret. The runbook must reference the canonical Vault key
    for the per-dept HMAC secret as well.
    """
    assert "Jira" in runbook_text, "Jira section header missing"
    assert "webhook" in runbook_text.lower(), "runbook subject ('webhook') missing"
    assert "URL" in runbook_text, "URL field marker missing"
    assert "Event" in runbook_text, "Events field marker missing"
    assert "Secret" in runbook_text or "secret" in runbook_text, (
        "Secret field marker missing"
    )
    assert "vault:webhooks/jira" in runbook_text, (
        "canonical Vault key for Jira HMAC secret (vault:webhooks/jira/...) missing"
    )


def test_runbook_contains_bitbucket_section(runbook_text: str) -> None:
    """The runbook SHALL also cover Bitbucket setup (R3.10 parity).

    R3.1 names ``X-Hub-Signature`` as the Bitbucket signature header, so the
    runbook must reference both ``Bitbucket`` and that header to be a
    complete operator artifact.
    """
    assert "Bitbucket" in runbook_text, "Bitbucket section missing from runbook"
    assert "X-Hub-Signature" in runbook_text, (
        "Bitbucket signature header X-Hub-Signature not documented"
    )


@pytest.mark.parametrize("event_type", REQUIRED_JIRA_EVENT_TYPES)
def test_runbook_includes_supported_jira_event_types(
    runbook_text: str, event_type: str
) -> None:
    """The runbook SHALL list every Jira event type the gateway supports (R3.2).

    These are the four event types the production handler accepts. If the
    runbook does not instruct an operator to subscribe to all four, the
    deployed webhook will silently miss event types and workflows will not
    fire.
    """
    assert event_type in runbook_text, (
        f"required Jira event type {event_type!r} (R3.2) not listed in runbook"
    )


@pytest.mark.parametrize("event_type", REQUIRED_BITBUCKET_EVENT_TYPES)
def test_runbook_includes_supported_bitbucket_event_types(
    runbook_text: str, event_type: str
) -> None:
    """The runbook SHALL list every Bitbucket event type the gateway supports (R3.3)."""
    assert event_type in runbook_text, (
        f"required Bitbucket event type {event_type!r} (R3.3) not listed in runbook"
    )
