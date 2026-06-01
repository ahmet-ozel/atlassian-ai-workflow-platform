"""Property test 14 — Pending Credentials Badge Invariant.

# Feature: platform-quick-fixes, Property 14: Pending Credentials Badge Invariant

Spec: ``platform-quick-fixes`` — Property 14.

**Validates: Requirements 3.8, 3.9**

Background
----------

The department detail page (``app/departments/[id]/page.tsx``) renders
a status badge based on whether the department has any bound credentials:

- **Zero credentials** (all bots have ``credential_ref: null``) →
  Yellow "Pending Credentials" badge + "Add Credential" button.
- **At least one credential** (any bot has ``credential_ref`` not null) →
  Green "Active" badge.

Strategy
--------

We use Hypothesis to generate random department detail payloads with
varying numbers of bots and credential_ref values. For each generated
payload we exercise the page's rendering logic (extracted into testable
pure functions) and assert the correct badge state is produced.

The test does NOT import React/Next.js. Instead it tests the **pure
logic** of the component's credential status determination, mirroring
the production code's branching exactly as found in the TSX source.
"""

from __future__ import annotations

from typing import Any, Final

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import pytest


# ---------------------------------------------------------------------------
# Extracted pure logic from app/departments/[id]/page.tsx
# ---------------------------------------------------------------------------
# The TSX component determines credential status with:
#
#   const hasCredentials =
#     state.kind === "ok" &&
#     state.detail.bots.some((bot) => bot.credential_ref != null);
#
# When hasCredentials is false → "Pending Credentials" badge + "Add Credential" button
# When hasCredentials is true  → "Active" badge
# ---------------------------------------------------------------------------


def has_credentials(bots: list[dict[str, Any]]) -> bool:
    """Determine if department has any bound credentials.

    Mirrors the TSX logic:
        state.detail.bots.some((bot) => bot.credential_ref != null)

    A bot's credential_ref is considered "bound" when it is not None.
    """
    return any(bot.get("credential_ref") is not None for bot in bots)


def determine_badge_state(detail: dict[str, Any] | None) -> str:
    """Determine which badge the department detail page should render.

    Returns one of:
        - "loading" — detail is None (still fetching)
        - "pending_credentials" — no bot has a bound credential_ref
        - "active" — at least one bot has a non-null credential_ref

    This mirrors the branching logic in the DepartmentDetailPage component.
    """
    if detail is None:
        return "loading"

    bots: list[dict[str, Any]] = detail.get("bots") or []

    if has_credentials(bots):
        return "active"

    return "pending_credentials"


def should_show_add_credential_button(detail: dict[str, Any] | None) -> bool:
    """Determine if the 'Add Credential' button should be rendered.

    The button is shown only when the badge state is 'pending_credentials'.
    """
    return determine_badge_state(detail) == "pending_credentials"


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Strategy for a service name.
_service_strategy = st.sampled_from(["jira", "bitbucket", "confluence"])

#: Strategy for a department ID.
_dept_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd", "Pd")),
    min_size=3,
    max_size=30,
)

#: Strategy for a display name.
_display_name_strategy = st.text(min_size=2, max_size=40)

#: Strategy for a credential_ref value (non-null — represents a bound credential).
_credential_ref_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_/",
    min_size=5,
    max_size=60,
)

#: Strategy for a bot with NO credential (credential_ref is null).
_bot_no_credential_strategy = st.builds(
    lambda service, account_id, username, deployment: {
        "service": service,
        "credential_ref": None,
        "account_id": account_id,
        "username": username,
        "deployment": deployment,
    },
    service=_service_strategy,
    account_id=st.one_of(st.none(), st.text(min_size=10, max_size=24)),
    username=st.one_of(st.none(), st.text(min_size=3, max_size=20)),
    deployment=st.one_of(st.none(), st.sampled_from(["cloud", "datacenter"])),
)

#: Strategy for a bot WITH a credential (credential_ref is not null).
_bot_with_credential_strategy = st.builds(
    lambda service, credential_ref, account_id, username, deployment: {
        "service": service,
        "credential_ref": credential_ref,
        "account_id": account_id,
        "username": username,
        "deployment": deployment,
    },
    service=_service_strategy,
    credential_ref=_credential_ref_strategy,
    account_id=st.one_of(st.none(), st.text(min_size=10, max_size=24)),
    username=st.one_of(st.none(), st.text(min_size=3, max_size=20)),
    deployment=st.one_of(st.none(), st.sampled_from(["cloud", "datacenter"])),
)

#: Strategy for a department detail with ZERO credentials (all bots have null credential_ref).
_zero_credentials_detail = st.builds(
    lambda dept_id, display_name, mode, bots: {
        "id": dept_id,
        "display_name": display_name,
        "mode": mode,
        "bots": bots,
    },
    dept_id=_dept_id_strategy,
    display_name=_display_name_strategy,
    mode=st.sampled_from(["active", "disabled", "setup"]),
    bots=st.lists(_bot_no_credential_strategy, min_size=0, max_size=5),
)

#: Strategy for a department detail with AT LEAST ONE credential.
_has_credentials_detail = st.builds(
    lambda dept_id, display_name, mode, no_cred_bots, cred_bots: {
        "id": dept_id,
        "display_name": display_name,
        "mode": mode,
        "bots": no_cred_bots + cred_bots,
    },
    dept_id=_dept_id_strategy,
    display_name=_display_name_strategy,
    mode=st.sampled_from(["active", "disabled", "setup"]),
    no_cred_bots=st.lists(_bot_no_credential_strategy, min_size=0, max_size=3),
    cred_bots=st.lists(_bot_with_credential_strategy, min_size=1, max_size=3),
)


# ---------------------------------------------------------------------------
# Property 14: Pending Credentials Badge — Zero Credentials Case
# ---------------------------------------------------------------------------


class TestPendingCredentialsBadge:
    """**Validates: Requirements 3.8, 3.9**

    For any department with zero bound credentials, the department detail
    page SHALL render a "Pending Credentials" badge (yellow) instead of
    "Active" badge, and SHALL show an "Add Credential" button.
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_zero_credentials_detail)
    def test_zero_credentials_shows_pending_badge(
        self, detail: dict[str, Any]
    ) -> None:
        """R3.8: Zero bound credentials → badge state is 'pending_credentials'."""
        state = determine_badge_state(detail)
        assert state == "pending_credentials", (
            f"Expected 'pending_credentials' for dept with zero credentials, "
            f"got '{state}'. Bots: {detail['bots']}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_zero_credentials_detail)
    def test_zero_credentials_shows_add_credential_button(
        self, detail: dict[str, Any]
    ) -> None:
        """R3.9: Zero bound credentials → 'Add Credential' button is rendered."""
        show_button = should_show_add_credential_button(detail)
        assert show_button is True, (
            "Expected 'Add Credential' button to be shown when no credentials "
            f"are bound. Bots: {detail['bots']}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_zero_credentials_detail)
    def test_zero_credentials_all_bots_have_null_credential_ref(
        self, detail: dict[str, Any]
    ) -> None:
        """R3.8: Confirm all bots in the zero-credentials case have null credential_ref."""
        bots = detail.get("bots") or []
        for bot in bots:
            assert bot.get("credential_ref") is None, (
                f"Expected null credential_ref for all bots in zero-credentials "
                f"scenario, but found: {bot}"
            )


# ---------------------------------------------------------------------------
# Property 14: Active Badge — At Least One Credential Case
# ---------------------------------------------------------------------------


class TestActiveBadge:
    """**Validates: Requirements 3.8, 3.9**

    For any department with at least one bound credential, the department
    detail page SHALL render an "Active" badge (green) and SHALL NOT show
    the "Add Credential" button.
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_has_credentials_detail)
    def test_has_credentials_shows_active_badge(
        self, detail: dict[str, Any]
    ) -> None:
        """R3.8: At least one credential → badge state is 'active'."""
        state = determine_badge_state(detail)
        assert state == "active", (
            f"Expected 'active' for dept with at least one credential, "
            f"got '{state}'. Bots: {detail['bots']}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_has_credentials_detail)
    def test_has_credentials_hides_add_credential_button(
        self, detail: dict[str, Any]
    ) -> None:
        """R3.9: At least one credential → 'Add Credential' button NOT shown."""
        show_button = should_show_add_credential_button(detail)
        assert show_button is False, (
            "Expected 'Add Credential' button to be hidden when credentials "
            f"are bound. Bots: {detail['bots']}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_has_credentials_detail)
    def test_has_credentials_at_least_one_non_null_ref(
        self, detail: dict[str, Any]
    ) -> None:
        """R3.8: Confirm at least one bot has a non-null credential_ref."""
        bots = detail.get("bots") or []
        has_any = any(bot.get("credential_ref") is not None for bot in bots)
        assert has_any, (
            "Expected at least one bot with non-null credential_ref in "
            f"has-credentials scenario. Bots: {bots}"
        )


# ---------------------------------------------------------------------------
# Property 14: Badge State Exhaustiveness and Mutual Exclusivity
# ---------------------------------------------------------------------------


class TestBadgeStateExhaustiveness:
    """Verify that the badge logic is exhaustive and mutually exclusive.

    **Validates: Requirements 3.8, 3.9**
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        detail=st.one_of(
            _zero_credentials_detail,
            _has_credentials_detail,
            st.just(None),
        )
    )
    def test_badge_state_is_always_one_of_three_values(
        self, detail: dict[str, Any] | None
    ) -> None:
        """Badge state is always one of the three defined values."""
        state = determine_badge_state(detail)
        assert state in ("loading", "pending_credentials", "active"), (
            f"Unexpected badge state: {state}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_zero_credentials_detail)
    def test_pending_implies_no_bound_credentials(
        self, detail: dict[str, Any]
    ) -> None:
        """'pending_credentials' state implies no bot has a bound credential."""
        state = determine_badge_state(detail)
        if state == "pending_credentials":
            bots = detail.get("bots") or []
            assert not has_credentials(bots), (
                "pending_credentials state should imply has_credentials is False"
            )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_has_credentials_detail)
    def test_active_implies_at_least_one_credential(
        self, detail: dict[str, Any]
    ) -> None:
        """'active' state implies at least one bot has a bound credential."""
        state = determine_badge_state(detail)
        if state == "active":
            bots = detail.get("bots") or []
            assert has_credentials(bots), (
                "active state should imply has_credentials is True"
            )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(detail=_has_credentials_detail)
    def test_add_credential_button_mutually_exclusive_with_active(
        self, detail: dict[str, Any]
    ) -> None:
        """'Add Credential' button and 'Active' badge are mutually exclusive."""
        state = determine_badge_state(detail)
        show_button = should_show_add_credential_button(detail)

        if state == "active":
            assert show_button is False
        elif state == "pending_credentials":
            assert show_button is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestPendingCredentialsEdgeCases:
    """Edge-case tests for the pending credentials badge logic.

    **Validates: Requirements 3.8, 3.9**
    """

    def test_empty_bots_list_shows_pending(self) -> None:
        """Department with empty bots list → pending credentials."""
        detail = {"id": "dept-1", "display_name": "Test", "mode": "active", "bots": []}
        assert determine_badge_state(detail) == "pending_credentials"
        assert should_show_add_credential_button(detail) is True

    def test_single_bot_null_credential_shows_pending(self) -> None:
        """Single bot with null credential_ref → pending credentials."""
        detail = {
            "id": "dept-2",
            "display_name": "Payment",
            "mode": "active",
            "bots": [
                {"service": "jira", "credential_ref": None, "account_id": None, "username": None, "deployment": None}
            ],
        }
        assert determine_badge_state(detail) == "pending_credentials"
        assert should_show_add_credential_button(detail) is True

    def test_single_bot_with_credential_shows_active(self) -> None:
        """Single bot with non-null credential_ref → active."""
        detail = {
            "id": "dept-3",
            "display_name": "Engineering",
            "mode": "active",
            "bots": [
                {
                    "service": "jira",
                    "credential_ref": "vault:creds/dept-3/jira",
                    "account_id": "5fc9e78dabcdef1234567890",
                    "username": "eng-bot",
                    "deployment": "cloud",
                }
            ],
        }
        assert determine_badge_state(detail) == "active"
        assert should_show_add_credential_button(detail) is False

    def test_multiple_bots_all_null_shows_pending(self) -> None:
        """Multiple bots all with null credential_ref → pending credentials."""
        detail = {
            "id": "dept-4",
            "display_name": "Marketing",
            "mode": "disabled",
            "bots": [
                {"service": "jira", "credential_ref": None, "account_id": None, "username": None, "deployment": None},
                {"service": "bitbucket", "credential_ref": None, "account_id": None, "username": None, "deployment": None},
                {"service": "confluence", "credential_ref": None, "account_id": None, "username": None, "deployment": None},
            ],
        }
        assert determine_badge_state(detail) == "pending_credentials"
        assert should_show_add_credential_button(detail) is True

    def test_mixed_bots_one_credential_shows_active(self) -> None:
        """Multiple bots, only one with credential → active."""
        detail = {
            "id": "dept-5",
            "display_name": "Sales",
            "mode": "active",
            "bots": [
                {"service": "jira", "credential_ref": "vault:creds/dept-5/jira", "account_id": "abc123", "username": "sales-bot", "deployment": "cloud"},
                {"service": "bitbucket", "credential_ref": None, "account_id": None, "username": None, "deployment": None},
                {"service": "confluence", "credential_ref": None, "account_id": None, "username": None, "deployment": None},
            ],
        }
        assert determine_badge_state(detail) == "active"
        assert should_show_add_credential_button(detail) is False

    def test_loading_state_when_detail_is_none(self) -> None:
        """When detail is None (still loading), badge state is 'loading'."""
        assert determine_badge_state(None) == "loading"
        assert should_show_add_credential_button(None) is False

    def test_missing_bots_field_shows_pending(self) -> None:
        """If 'bots' field is missing entirely, treat as pending credentials."""
        detail = {"id": "dept-6", "display_name": "Ops", "mode": "active"}
        assert determine_badge_state(detail) == "pending_credentials"
        assert should_show_add_credential_button(detail) is True

    def test_none_bots_field_shows_pending(self) -> None:
        """If 'bots' field is None, treat as pending credentials."""
        detail = {"id": "dept-7", "display_name": "HR", "mode": "active", "bots": None}
        assert determine_badge_state(detail) == "pending_credentials"
        assert should_show_add_credential_button(detail) is True
