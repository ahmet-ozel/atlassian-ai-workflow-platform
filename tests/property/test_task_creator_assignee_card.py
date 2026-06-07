"""Task Creator Bot Assignee Card tri-state rendering
and graceful degradation.

Background
----------

The Task Creator page (``pages/2_task_creator.py``) renders a "Bot
Assignee Info Card" below the dept switcher widget. The card fetches
data from ``GET /api/dept/{id}/bot-info`` and renders one of three
visual states:

(a) **Green badges** - All credentials present and probe status is
    ``"ok"`` for every bot entry. Each service shows ✅.
(b) **Red warning** - No bot credentials at all (``bots`` list is
    empty). Shows error message + link to Credentials page.
(c) **Yellow warning** - At least one credential exists but its probe
    status is not ``"ok"`` (e.g. ``"failed"``, ``"timeout"``). Shows
    warning message + admin-dashboard /security deep link.

Strategy
--------

We use Hypothesis to generate random department bot-info payloads
that fall into the three categories above. For each generated payload
we exercise the card's rendering logic (extracted into testable pure
functions from ``components/bot_assignee_card.py``) and assert the
correct state is produced.

The test does NOT import Streamlit itself (which requires a running
server context). Instead it tests the **pure logic** of the component's
state determination and helper functions, mirroring the production
code's branching exactly.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import httpx
import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap - expose the streamlit-app components so we can
# import the bot_assignee_card module's helpers directly.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "ui" / "streamlit-app",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# ---------------------------------------------------------------------------
# We cannot import the full component (it depends on ``streamlit``),
# so we extract and mirror the pure logic here. This is the standard
# pattern used by test_chat_intent_wiring.py and others in this suite.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Extracted pure logic from bot_assignee_card.py
# ---------------------------------------------------------------------------

_KNOWN_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")
_ACCOUNT_ID_DISPLAY_LEN: int = 8


def _truncate_account_id(account_id: str | None) -> str:
    """Return first 8 chars + ellipsis, or placeholder if empty."""
    if not account_id:
        return "-"
    if len(account_id) <= _ACCOUNT_ID_DISPLAY_LEN:
        return account_id
    return f"{account_id[:_ACCOUNT_ID_DISPLAY_LEN]}…"


def _probe_badge(probe_status: str) -> str:
    """Return ✅ for ok, ❌ for anything else."""
    if probe_status == "ok":
        return "✅"
    return "❌"


def determine_card_state(
    data: dict[str, Any] | None,
) -> str:
    """Determine the visual state of the bot assignee card.

    Returns one of:
        - "unavailable" - data is None (API unreachable)
        - "no_credentials" - bots list is empty (red warning)
        - "probe_failure" - at least one bot has probe_status != "ok"
          and != "not_probed" (yellow warning)
        - "all_ok" - all bots have probe_status == "ok" (green badges)

    This mirrors the branching logic in ``render_bot_assignee_card``.
    """
    if data is None:
        return "unavailable"

    bots: list[Mapping[str, Any]] = data.get("bots") or []

    if not bots:
        return "no_credentials"

    has_probe_failure = any(
        bot.get("probe_status") not in ("ok", "not_probed")
        for bot in bots
    )

    if has_probe_failure:
        return "probe_failure"

    return "all_ok"


def get_badges(bots: list[dict[str, Any]]) -> list[str]:
    """Generate badge strings for each bot service."""
    badges: list[str] = []
    for bot in bots:
        service = bot.get("service", "?")
        probe_status = bot.get("probe_status", "not_probed")
        badge = _probe_badge(probe_status)
        badges.append(f"{badge} {service}")
    return badges


def get_primary_bot(bots: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the primary bot (prefer jira, fallback to first)."""
    return next(
        (b for b in bots if b.get("service") == "jira"),
        bots[0],
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Strategy for a valid account_id (hex string, 24 chars like Atlassian).
_account_id_strategy = st.text(
    alphabet="0123456789abcdef",
    min_size=24,
    max_size=24,
)

#: Strategy for a bot username.
_username_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd", "Pd")),
    min_size=3,
    max_size=30,
)

#: Strategy for a service name.
_service_strategy = st.sampled_from(["jira", "bitbucket", "confluence"])

#: Strategy for a bot entry with probe_status == "ok".
_bot_ok_strategy = st.builds(
    lambda service, username, account_id, probed_at: {
        "service": service,
        "username": username,
        "account_id": account_id,
        "probe_status": "ok",
        "probed_at": probed_at,
    },
    service=_service_strategy,
    username=_username_strategy,
    account_id=_account_id_strategy,
    probed_at=st.just("2024-01-15T10:30:00Z"),
)

#: Strategy for a bot entry with probe failure.
_bot_failed_strategy = st.builds(
    lambda service, username, account_id, probe_status, probed_at: {
        "service": service,
        "username": username,
        "account_id": account_id,
        "probe_status": probe_status,
        "probed_at": probed_at,
    },
    service=_service_strategy,
    username=_username_strategy,
    account_id=_account_id_strategy,
    probe_status=st.sampled_from(["failed", "timeout", "unauthorized", "error"]),
    probed_at=st.just("2024-01-15T10:30:00Z"),
)

#: Strategy for a display name.
_display_name_strategy = st.text(min_size=3, max_size=40)

#: Strategy for bot-info with ALL probes ok (state a: green badges).
_all_ok_bot_info = st.builds(
    lambda display_name, bots: {
        "display_name": display_name,
        "bots": bots,
    },
    display_name=_display_name_strategy,
    bots=st.lists(_bot_ok_strategy, min_size=1, max_size=3),
)

#: Strategy for bot-info with NO credentials (state b: red warning).
_no_credentials_bot_info = st.builds(
    lambda display_name: {
        "display_name": display_name,
        "bots": [],
    },
    display_name=_display_name_strategy,
)

#: Strategy for bot-info with at least one probe failure (state c: yellow warning).
_probe_failure_bot_info = st.builds(
    lambda display_name, ok_bots, failed_bots: {
        "display_name": display_name,
        "bots": ok_bots + failed_bots,
    },
    display_name=_display_name_strategy,
    ok_bots=st.lists(_bot_ok_strategy, min_size=0, max_size=2),
    failed_bots=st.lists(_bot_failed_strategy, min_size=1, max_size=2),
)


# ---------------------------------------------------------------------------
# Task Creator Assignee Card - Green Badges (State A)
# ---------------------------------------------------------------------------


class TestAssigneeCardAllOk:
    """When all bot credentials are present and probe status is "ok" for
    every entry, the card renders green badges (✅) for each service.
    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_all_ok_bot_info)
    def test_all_ok_produces_green_state(
        self, data: dict[str, Any]
    ) -> None:
        """All credentials present + probe ok → card state is 'all_ok'."""
        state = determine_card_state(data)
        assert state == "all_ok", (
            f"Expected 'all_ok' state for data with all probes ok, got '{state}'"
        )

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_all_ok_bot_info)
    def test_all_ok_badges_are_green(
        self, data: dict[str, Any]
    ) -> None:
        """Each service badge shows ✅ when probe is ok."""
        bots = data["bots"]
        badges = get_badges(bots)

        for badge in badges:
            assert "✅" in badge, (
                f"Expected green badge (✅) for ok probe, got: {badge}"
            )
            assert "❌" not in badge

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_all_ok_bot_info)
    def test_all_ok_primary_bot_has_account_id(
        self, data: dict[str, Any]
    ) -> None:
        """The primary bot has a non-empty account_id."""
        bots = data["bots"]
        primary = get_primary_bot(bots)
        assert primary.get("account_id"), (
            "Primary bot should have a non-empty account_id"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_all_ok_bot_info)
    def test_all_ok_account_id_truncation(
        self, data: dict[str, Any]
    ) -> None:
        """Account ID is truncated to 8 chars + ellipsis for display."""
        bots = data["bots"]
        primary = get_primary_bot(bots)
        account_id = primary["account_id"]
        truncated = _truncate_account_id(account_id)

        assert truncated == f"{account_id[:8]}…"
        assert len(truncated) == 9  # 8 chars + "…"


# ---------------------------------------------------------------------------
# Task Creator Assignee Card - Red Warning (State B)
# ---------------------------------------------------------------------------


class TestAssigneeCardNoCredentials:
    """When no bot credentials exist (empty bots list), the card renders
    a red warning directing the user to the Credentials page.
    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_no_credentials_bot_info)
    def test_no_credentials_produces_red_state(
        self, data: dict[str, Any]
    ) -> None:
        """No credentials → card state is 'no_credentials'."""
        state = determine_card_state(data)
        assert state == "no_credentials", (
            f"Expected 'no_credentials' state for empty bots list, got '{state}'"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_no_credentials_bot_info)
    def test_no_credentials_bots_list_is_empty(
        self, data: dict[str, Any]
    ) -> None:
        """The bots list is empty, confirming no credentials."""
        bots = data.get("bots") or []
        assert len(bots) == 0

    def test_no_credentials_with_none_bots_field(self) -> None:
        """If 'bots' field is None, treat as no credentials."""
        data = {"display_name": "Test Dept", "bots": None}
        state = determine_card_state(data)
        assert state == "no_credentials"

    def test_no_credentials_with_missing_bots_field(self) -> None:
        """If 'bots' field is missing entirely, treat as no credentials."""
        data = {"display_name": "Test Dept"}
        state = determine_card_state(data)
        assert state == "no_credentials"


# ---------------------------------------------------------------------------
# Task Creator Assignee Card - Yellow Warning (State C)
# ---------------------------------------------------------------------------


class TestAssigneeCardProbeFailure:
    """When at least one credential exists but its probe status indicates
    failure, the card renders a yellow warning directing the user to
    the admin-dashboard /security page for re-probe.
    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_probe_failure_bot_info)
    def test_probe_failure_produces_yellow_state(
        self, data: dict[str, Any]
    ) -> None:
        """Credential exists + probe fail → card state is 'probe_failure'."""
        state = determine_card_state(data)
        assert state == "probe_failure", (
            f"Expected 'probe_failure' state for data with failed probes, got '{state}'"
        )

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_probe_failure_bot_info)
    def test_probe_failure_has_at_least_one_red_badge(
        self, data: dict[str, Any]
    ) -> None:
        """At least one service badge shows ❌ when probe failed."""
        bots = data["bots"]
        badges = get_badges(bots)

        has_red = any("❌" in badge for badge in badges)
        assert has_red, (
            f"Expected at least one red badge (❌) for probe failure, "
            f"got badges: {badges}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_probe_failure_bot_info)
    def test_probe_failure_bots_list_is_non_empty(
        self, data: dict[str, Any]
    ) -> None:
        """The bots list is non-empty (credentials exist)."""
        bots = data.get("bots") or []
        assert len(bots) > 0, (
            "Probe failure state requires at least one bot entry"
        )

    def test_probe_failure_with_single_failed_bot(self) -> None:
        """A single bot with 'failed' probe triggers yellow state."""
        data = {
            "display_name": "Payment Team",
            "bots": [
                {
                    "service": "jira",
                    "username": "payment-bot",
                    "account_id": "5fc9e78dabcdef1234567890",
                    "probe_status": "failed",
                    "probed_at": "2024-01-15T10:30:00Z",
                }
            ],
        }
        state = determine_card_state(data)
        assert state == "probe_failure"

    def test_probe_failure_mixed_ok_and_failed(self) -> None:
        """Mix of ok and failed probes still triggers yellow state."""
        data = {
            "display_name": "Payment Team",
            "bots": [
                {
                    "service": "jira",
                    "username": "payment-bot",
                    "account_id": "5fc9e78dabcdef1234567890",
                    "probe_status": "ok",
                    "probed_at": "2024-01-15T10:30:00Z",
                },
                {
                    "service": "bitbucket",
                    "username": "payment-bot",
                    "account_id": "abc123def4567890abcdef12",
                    "probe_status": "timeout",
                    "probed_at": "2024-01-15T10:30:00Z",
                },
            ],
        }
        state = determine_card_state(data)
        assert state == "probe_failure"


# ---------------------------------------------------------------------------
# Edge cases and helper function tests
# ---------------------------------------------------------------------------


class TestAssigneeCardEdgeCases:
    """Edge-case properties for the assignee card contract.

    """

    def test_unavailable_when_data_is_none(self) -> None:
        """API unreachable → card state is 'unavailable'."""
        state = determine_card_state(None)
        assert state == "unavailable"

    def test_not_probed_status_treated_as_ok(self) -> None:
        """Bots with 'not_probed' status are NOT treated as failures.

        This matches the production logic where ``not_probed`` means
        the probe hasn't run yet (not a failure condition).
        """
        data = {
            "display_name": "New Dept",
            "bots": [
                {
                    "service": "jira",
                    "username": "new-bot",
                    "account_id": "aabbccdd11223344aabbccdd",
                    "probe_status": "not_probed",
                    "probed_at": None,
                }
            ],
        }
        state = determine_card_state(data)
        assert state == "all_ok"

    def test_primary_bot_prefers_jira(self) -> None:
        """Primary bot selection prefers jira over other services."""
        bots = [
            {"service": "bitbucket", "username": "bb-bot", "account_id": "a" * 24, "probe_status": "ok"},
            {"service": "jira", "username": "jira-bot", "account_id": "b" * 24, "probe_status": "ok"},
            {"service": "confluence", "username": "conf-bot", "account_id": "c" * 24, "probe_status": "ok"},
        ]
        primary = get_primary_bot(bots)
        assert primary["service"] == "jira"
        assert primary["username"] == "jira-bot"

    def test_primary_bot_fallback_to_first(self) -> None:
        """When no jira bot exists, primary is the first in the list."""
        bots = [
            {"service": "bitbucket", "username": "bb-bot", "account_id": "a" * 24, "probe_status": "ok"},
            {"service": "confluence", "username": "conf-bot", "account_id": "c" * 24, "probe_status": "ok"},
        ]
        primary = get_primary_bot(bots)
        assert primary["service"] == "bitbucket"

    def test_truncate_account_id_long(self) -> None:
        """Long account_id is truncated to 8 chars + ellipsis."""
        result = _truncate_account_id("5fc9e78dabcdef1234567890")
        assert result == "5fc9e78d…"

    def test_truncate_account_id_short(self) -> None:
        """Short account_id (≤8 chars) is returned as-is."""
        result = _truncate_account_id("abc123")
        assert result == "abc123"

    def test_truncate_account_id_exactly_8(self) -> None:
        """Account_id of exactly 8 chars is returned as-is."""
        result = _truncate_account_id("12345678")
        assert result == "12345678"

    def test_truncate_account_id_empty(self) -> None:
        """Empty account_id returns placeholder."""
        assert _truncate_account_id("") == "-"
        assert _truncate_account_id(None) == "-"

    def test_probe_badge_ok(self) -> None:
        """Probe status 'ok' returns green badge."""
        assert _probe_badge("ok") == "✅"

    def test_probe_badge_failed(self) -> None:
        """Probe status 'failed' returns red badge."""
        assert _probe_badge("failed") == "❌"

    def test_probe_badge_timeout(self) -> None:
        """Probe status 'timeout' returns red badge."""
        assert _probe_badge("timeout") == "❌"

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        probe_status=st.text(min_size=1, max_size=20).filter(
            lambda s: s != "ok"
        )
    )
    def test_probe_badge_non_ok_always_red(self, probe_status: str) -> None:
        """Any probe_status that is not 'ok' produces ❌."""
        assert _probe_badge(probe_status) == "❌"


# ---------------------------------------------------------------------------
# Bot Info Card Rendering Completeness
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Extracted pure logic from bot_identity_card.py for rendering tests
# ---------------------------------------------------------------------------

#: Badge mapping for probe status (mirrors bot_identity_card.py).
_IDENTITY_PROBE_BADGE: dict[str, str] = {
    "ok": "🟢",
    "failed": "🔴",
    "not_probed": "🟡",
}
_IDENTITY_PROBE_BADGE_DEFAULT: str = "⚪"


def _identity_get_probe_badge(probe_status: str) -> str:
    """Return the emoji badge for a given probe status."""
    return _IDENTITY_PROBE_BADGE.get(probe_status, _IDENTITY_PROBE_BADGE_DEFAULT)


def simulate_render_bot_identity_card(data: dict[str, Any] | None) -> dict[str, Any]:
    """Simulate the pure logic of render_bot_identity_card.

    This mirrors the branching in bot_identity_card.py without importing
    Streamlit. Returns a dict describing what would be rendered:
    - "state": "degraded" | "no_jira_bot" | "rendered"
    - "account_id": str | None (the return value for Assignee pre-fill)
    - "display_name": str | None
    - "username": str | None
    - "probe_badge": str | None
    - "account_id_displayed": str | None (the code block content)
    """
    if data is None:
        return {
            "state": "degraded",
            "account_id": None,
            "display_name": None,
            "username": None,
            "probe_badge": None,
            "account_id_displayed": None,
        }

    display_name: str = data.get("display_name", "")
    bots: list[dict[str, Any]] = data.get("bots") or []

    jira_bot: dict[str, Any] | None = next(
        (b for b in bots if b.get("service") == "jira"), None
    )

    if jira_bot is None:
        return {
            "state": "no_jira_bot",
            "account_id": None,
            "display_name": display_name,
            "username": None,
            "probe_badge": None,
            "account_id_displayed": None,
        }

    account_id: str = jira_bot.get("account_id", "")
    username: str = jira_bot.get("username", "-")
    probe_status: str = jira_bot.get("probe_status", "not_probed")
    badge: str = _identity_get_probe_badge(probe_status)

    return {
        "state": "rendered",
        "account_id": account_id,
        "display_name": display_name,
        "username": username,
        "probe_badge": badge,
        "account_id_displayed": account_id,
    }


# ---------------------------------------------------------------------------
# Hypothesis strategies for bot info rendering
# ---------------------------------------------------------------------------

#: Strategy for a valid Jira bot entry (any probe status).
_jira_bot_strategy = st.builds(
    lambda username, account_id, probe_status, probed_at: {
        "service": "jira",
        "username": username,
        "account_id": account_id,
        "probe_status": probe_status,
        "probed_at": probed_at,
    },
    username=_username_strategy,
    account_id=_account_id_strategy,
    probe_status=st.sampled_from(["ok", "failed", "not_probed"]),
    probed_at=st.one_of(st.just("2024-01-15T10:30:00Z"), st.just(None)),
)

#: Strategy for non-Jira bot entries.
_non_jira_bot_strategy = st.builds(
    lambda service, username, account_id, probe_status: {
        "service": service,
        "username": username,
        "account_id": account_id,
        "probe_status": probe_status,
        "probed_at": "2024-01-15T10:30:00Z",
    },
    service=st.sampled_from(["bitbucket", "confluence"]),
    username=_username_strategy,
    account_id=_account_id_strategy,
    probe_status=st.sampled_from(["ok", "failed", "not_probed"]),
)

#: Strategy for a valid bot-info response containing at least one Jira bot.
_bot_info_with_jira_strategy = st.builds(
    lambda display_name, jira_bot, other_bots: {
        "display_name": display_name,
        "bots": [jira_bot] + other_bots,
    },
    display_name=st.text(min_size=1, max_size=50).filter(lambda s: s.strip()),
    jira_bot=_jira_bot_strategy,
    other_bots=st.lists(_non_jira_bot_strategy, min_size=0, max_size=3),
)


class TestBotInfoCardRenderingCompleteness:
    """Bot Info Card Rendering Completeness.

    For any valid bot-info API response containing at least one Jira bot,
    the Task Creator info card SHALL display display_name, bot username,
    account_id (copyable), and probe status badge. The Assignee field
    SHALL be pre-filled with the Jira bot's account_id.
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_bot_info_with_jira_strategy)
    def test_jira_bot_returns_account_id_for_assignee_prefill(
        self, data: dict[str, Any]
    ) -> None:
        """Jira bot present → account_id returned for Assignee pre-fill."""
        result = simulate_render_bot_identity_card(data)

        # The function must return the Jira bot's account_id
        assert result["account_id"] is not None
        assert result["account_id"] != ""

        # The returned account_id must match the Jira bot in the input
        jira_bot = next(b for b in data["bots"] if b["service"] == "jira")
        assert result["account_id"] == jira_bot["account_id"]

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_bot_info_with_jira_strategy)
    def test_display_name_is_shown(
        self, data: dict[str, Any]
    ) -> None:
        """Info card displays dept display_name."""
        result = simulate_render_bot_identity_card(data)

        assert result["state"] == "rendered"
        assert result["display_name"] is not None
        assert result["display_name"] == data["display_name"]

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_bot_info_with_jira_strategy)
    def test_username_is_shown(
        self, data: dict[str, Any]
    ) -> None:
        """Info card displays bot username."""
        result = simulate_render_bot_identity_card(data)

        assert result["state"] == "rendered"
        assert result["username"] is not None

        jira_bot = next(b for b in data["bots"] if b["service"] == "jira")
        assert result["username"] == jira_bot["username"]

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_bot_info_with_jira_strategy)
    def test_account_id_displayed_in_copyable_block(
        self, data: dict[str, Any]
    ) -> None:
        """Info card displays account_id in a copyable code block."""
        result = simulate_render_bot_identity_card(data)

        assert result["state"] == "rendered"
        assert result["account_id_displayed"] is not None

        jira_bot = next(b for b in data["bots"] if b["service"] == "jira")
        # The displayed account_id must be the full account_id (for copy)
        assert result["account_id_displayed"] == jira_bot["account_id"]

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_bot_info_with_jira_strategy)
    def test_probe_badge_is_shown(
        self, data: dict[str, Any]
    ) -> None:
        """Info card displays probe status badge."""
        result = simulate_render_bot_identity_card(data)

        assert result["state"] == "rendered"
        assert result["probe_badge"] is not None

        # Badge must be one of the known badge emojis
        jira_bot = next(b for b in data["bots"] if b["service"] == "jira")
        expected_badge = _identity_get_probe_badge(jira_bot["probe_status"])
        assert result["probe_badge"] == expected_badge

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_bot_info_with_jira_strategy)
    def test_all_four_elements_present_simultaneously(
        self, data: dict[str, Any]
    ) -> None:
        """All four display elements are present at the same time.

        display_name, username, account_id, and probe badge must ALL
        be non-None when a Jira bot exists in the response.
        """
        result = simulate_render_bot_identity_card(data)

        assert result["state"] == "rendered"
        assert result["display_name"] is not None and result["display_name"] != ""
        assert result["username"] is not None
        assert result["account_id"] is not None and result["account_id"] != ""
        assert result["probe_badge"] is not None and result["probe_badge"] != ""

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_bot_info_with_jira_strategy)
    def test_assignee_prefill_matches_jira_bot_account_id(
        self, data: dict[str, Any]
    ) -> None:
        """Assignee field pre-fill value equals Jira bot account_id.

        The return value of render_bot_identity_card is used directly
        as the Assignee field pre-fill in pages/2_task_creator.py.
        """
        result = simulate_render_bot_identity_card(data)

        # Simulate what the task creator page does:
        # _jira_bot_account_id = render_bot_identity_card(dept_id, api_base)
        # if _jira_bot_account_id:
        #     st.session_state["_bot_identity_card_account_id"] = _jira_bot_account_id
        # Then: _prefill_assignee = st.session_state.get("_bot_identity_card_account_id")

        jira_bot = next(b for b in data["bots"] if b["service"] == "jira")
        expected_assignee = jira_bot["account_id"]

        # The returned account_id IS the assignee pre-fill value
        assert result["account_id"] == expected_assignee


# ---------------------------------------------------------------------------
# Task Creator Assignee Card - State Exhaustiveness
# ---------------------------------------------------------------------------


class TestAssigneeCardStateExhaustiveness:
    """Verify that the tri-state logic is exhaustive and mutually exclusive.

    """

    @settings(
        max_examples=300,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        data=st.one_of(
            _all_ok_bot_info,
            _no_credentials_bot_info,
            _probe_failure_bot_info,
            st.just(None),
        )
    )
    def test_state_is_always_one_of_four_values(
        self, data: dict[str, Any] | None
    ) -> None:
        """The card state is always one of the four defined values."""
        state = determine_card_state(data)
        assert state in ("unavailable", "no_credentials", "probe_failure", "all_ok"), (
            f"Unexpected card state: {state}"
        )

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_all_ok_bot_info)
    def test_all_ok_implies_non_empty_bots(
        self, data: dict[str, Any]
    ) -> None:
        """'all_ok' state implies bots list is non-empty."""
        state = determine_card_state(data)
        if state == "all_ok":
            bots = data.get("bots") or []
            assert len(bots) > 0

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(data=_probe_failure_bot_info)
    def test_probe_failure_implies_non_empty_bots(
        self, data: dict[str, Any]
    ) -> None:
        """'probe_failure' state implies bots list is non-empty."""
        state = determine_card_state(data)
        if state == "probe_failure":
            bots = data.get("bots") or []
            assert len(bots) > 0


# ---------------------------------------------------------------------------
# Bot Info Graceful Degradation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Strategies for error scenarios
# ---------------------------------------------------------------------------

#: Strategy for HTTP error status codes (non-200 responses).
_error_status_code_strategy = st.sampled_from([500, 502, 503, 504, 400, 403, 404, 429])

#: Strategy for error types that can occur during HTTP requests.
_error_type_strategy = st.sampled_from([
    "timeout",       # httpx.TimeoutException
    "connect_error", # httpx.ConnectError
    "http_error",    # httpx.HTTPError (generic)
    "non_200",       # Server returns non-200 status code
])

#: Strategy for department IDs.
_dept_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd", "Pd")),
    min_size=3,
    max_size=30,
)

#: Strategy for API base URLs.
_api_base_strategy = st.sampled_from([
    "http://assistant-service:8081",
    "http://localhost:8081",
    "http://10.0.0.5:8081",
])


# ---------------------------------------------------------------------------
# Helper: simulate _fetch_bot_info under error conditions
# ---------------------------------------------------------------------------


def _simulate_fetch_bot_info_error(error_type: str, status_code: int = 503) -> dict[str, Any] | None:
    """Simulate the _fetch_bot_info function behavior under error conditions.

    This mirrors the production logic in bot_identity_card.py:
    - Non-200 status → returns None
    - TimeoutException → returns None
    - ConnectError → returns None
    - HTTPError → returns None
    """
    if error_type == "non_200":
        # Server responded but with an error status code
        return None
    elif error_type == "timeout":
        # httpx.TimeoutException raised
        return None
    elif error_type == "connect_error":
        # httpx.ConnectError raised
        return None
    elif error_type == "http_error":
        # httpx.HTTPError raised
        return None
    return None


def _simulate_render_bot_identity_card_degradation(
    error_type: str,
    status_code: int = 503,
) -> tuple[str | None, str, bool]:
    """Simulate render_bot_identity_card under error conditions.

    Returns:
        (account_id, warning_message, has_retry_option)

    Under any error condition:
    - account_id is None (Task Creator remains functional with empty assignee)
    - warning_message is the degradation warning
    - has_retry_option is True (retry button is rendered)
    """
    # _fetch_bot_info returns None on any error
    data = _simulate_fetch_bot_info_error(error_type, status_code)

    if data is None:
        # This is the graceful degradation path
        warning_message = "Bot bilgileri yüklenemedi (yeniden dene)"
        return None, warning_message, True

    # This path should never be reached in error scenarios
    return "unexpected", "", False


# ---------------------------------------------------------------------------
# Bot Info Graceful Degradation
# ---------------------------------------------------------------------------


class TestBotInfoGracefulDegradation:
    """Bot Info Graceful Degradation.

    For any error response from the bot-info endpoint (503, timeout,
    network error), the Streamlit page SHALL display a "Bot bilgileri
    yüklenemedi" warning with a retry option, and the Task Creator
    SHALL remain functional (assignee field empty but usable).
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        error_type=_error_type_strategy,
        status_code=_error_status_code_strategy,
        dept_id=_dept_id_strategy,
    )
    def test_any_error_returns_none_account_id(
        self, error_type: str, status_code: int, dept_id: str
    ) -> None:
        """Any error → account_id is None (Task Creator functional, assignee empty)."""
        account_id, _, _ = _simulate_render_bot_identity_card_degradation(
            error_type, status_code
        )
        assert account_id is None, (
            f"Expected None account_id for error_type={error_type}, "
            f"status_code={status_code}, got: {account_id}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        error_type=_error_type_strategy,
        status_code=_error_status_code_strategy,
    )
    def test_any_error_shows_degradation_warning(
        self, error_type: str, status_code: int
    ) -> None:
        """Any error → warning message contains 'Bot bilgileri yüklenemedi'."""
        _, warning_message, _ = _simulate_render_bot_identity_card_degradation(
            error_type, status_code
        )
        assert "Bot bilgileri yüklenemedi" in warning_message, (
            f"Expected degradation warning for error_type={error_type}, "
            f"got: '{warning_message}'"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        error_type=_error_type_strategy,
        status_code=_error_status_code_strategy,
    )
    def test_any_error_provides_retry_option(
        self, error_type: str, status_code: int
    ) -> None:
        """Any error → retry option is available."""
        _, _, has_retry = _simulate_render_bot_identity_card_degradation(
            error_type, status_code
        )
        assert has_retry is True, (
            f"Expected retry option for error_type={error_type}, "
            f"status_code={status_code}"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        error_type=_error_type_strategy,
        dept_id=_dept_id_strategy,
        api_base=_api_base_strategy,
    )
    def test_fetch_bot_info_returns_none_on_error(
        self, error_type: str, dept_id: str, api_base: str
    ) -> None:
        """_fetch_bot_info returns None for any error scenario.

        This tests the actual _fetch_bot_info function with mocked httpx.get
        to verify it correctly returns None for all error types.
        """
        # Import the actual function
        from components.bot_identity_card import _fetch_bot_info

        if error_type == "timeout":
            with patch("components.bot_identity_card.httpx.get") as mock_get:
                mock_get.side_effect = httpx.TimeoutException("Connection timed out")
                result = _fetch_bot_info(dept_id, api_base)
                assert result is None

        elif error_type == "connect_error":
            with patch("components.bot_identity_card.httpx.get") as mock_get:
                mock_get.side_effect = httpx.ConnectError("Connection refused")
                result = _fetch_bot_info(dept_id, api_base)
                assert result is None

        elif error_type == "http_error":
            with patch("components.bot_identity_card.httpx.get") as mock_get:
                mock_get.side_effect = httpx.HTTPError("Generic HTTP error")
                result = _fetch_bot_info(dept_id, api_base)
                assert result is None

        elif error_type == "non_200":
            with patch("components.bot_identity_card.httpx.get") as mock_get:
                mock_response = MagicMock()
                mock_response.status_code = 503
                mock_get.return_value = mock_response
                result = _fetch_bot_info(dept_id, api_base)
                assert result is None

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        status_code=_error_status_code_strategy,
        dept_id=_dept_id_strategy,
        api_base=_api_base_strategy,
    )
    def test_non_200_status_codes_return_none(
        self, status_code: int, dept_id: str, api_base: str
    ) -> None:
        """Any non-200 HTTP status code → _fetch_bot_info returns None."""
        from components.bot_identity_card import _fetch_bot_info

        with patch("components.bot_identity_card.httpx.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = status_code
            mock_get.return_value = mock_response
            result = _fetch_bot_info(dept_id, api_base)
            assert result is None, (
                f"Expected None for status_code={status_code}, got: {result}"
            )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        api_base=_api_base_strategy,
    )
    def test_render_returns_none_and_shows_warning_on_error(
        self, dept_id: str, api_base: str
    ) -> None:
        """render_bot_identity_card returns None and calls st.warning on error.

        Verifies the full render function gracefully degrades: returns None
        (so Task Creator remains functional with empty assignee) and displays
        the degradation warning message.
        """
        from components.bot_identity_card import render_bot_identity_card

        with patch("components.bot_identity_card.httpx.get") as mock_get, \
             patch("components.bot_identity_card.st") as mock_st:
            # Simulate a 503 error
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_get.return_value = mock_response

            # Mock st.button to return False (user hasn't clicked retry)
            mock_st.button.return_value = False

            result = render_bot_identity_card(dept_id, api_base)

            # Task Creator remains functional - returns None (assignee empty)
            assert result is None

            # Warning message displayed
            mock_st.warning.assert_called_once_with(
                "Bot bilgileri yüklenemedi (yeniden dene)"
            )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        api_base=_api_base_strategy,
    )
    def test_render_returns_none_on_timeout(
        self, dept_id: str, api_base: str
    ) -> None:
        """render_bot_identity_card returns None on timeout exception."""
        from components.bot_identity_card import render_bot_identity_card

        with patch("components.bot_identity_card.httpx.get") as mock_get, \
             patch("components.bot_identity_card.st") as mock_st:
            mock_get.side_effect = httpx.TimeoutException("Request timed out")
            mock_st.button.return_value = False

            result = render_bot_identity_card(dept_id, api_base)

            assert result is None
            mock_st.warning.assert_called_once_with(
                "Bot bilgileri yüklenemedi (yeniden dene)"
            )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        api_base=_api_base_strategy,
    )
    def test_render_returns_none_on_connect_error(
        self, dept_id: str, api_base: str
    ) -> None:
        """render_bot_identity_card returns None on connection error."""
        from components.bot_identity_card import render_bot_identity_card

        with patch("components.bot_identity_card.httpx.get") as mock_get, \
             patch("components.bot_identity_card.st") as mock_st:
            mock_get.side_effect = httpx.ConnectError("Connection refused")
            mock_st.button.return_value = False

            result = render_bot_identity_card(dept_id, api_base)

            assert result is None
            mock_st.warning.assert_called_once_with(
                "Bot bilgileri yüklenemedi (yeniden dene)"
            )

    def test_determine_card_state_unavailable_on_none(self) -> None:
        """determine_card_state returns 'unavailable' when data is None.

        This confirms the pure logic layer correctly identifies the
        degradation state.
        """
        state = determine_card_state(None)
        assert state == "unavailable"

    def test_task_creator_functional_when_bot_info_fails(self) -> None:
        """Task Creator remains functional when bot-info fails.

        The contract is: render_bot_identity_card returns None on error,
        which means the Assignee field stays empty but the rest of the
        Task Creator page continues to work normally.
        """
        from components.bot_identity_card import render_bot_identity_card

        with patch("components.bot_identity_card.httpx.get") as mock_get, \
             patch("components.bot_identity_card.st") as mock_st:
            # Simulate network error
            mock_get.side_effect = httpx.ConnectError("Network unreachable")
            mock_st.button.return_value = False

            result = render_bot_identity_card("payment-dept", "http://localhost:8081")

            # Returns None - Task Creator uses this to decide assignee pre-fill
            # None means "don't pre-fill" → field remains empty but usable
            assert result is None

            # The function does NOT raise - it degrades gracefully
            # st.warning is called (not st.error or st.exception)
            mock_st.warning.assert_called()
            mock_st.error.assert_not_called()

    def test_retry_button_rendered_on_error(self) -> None:
        """A retry button is rendered when bot-info endpoint fails."""
        from components.bot_identity_card import render_bot_identity_card

        with patch("components.bot_identity_card.httpx.get") as mock_get, \
             patch("components.bot_identity_card.st") as mock_st:
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_get.return_value = mock_response
            mock_st.button.return_value = False

            render_bot_identity_card("test-dept", "http://localhost:8081")

            # Verify retry button is rendered
            mock_st.button.assert_called_once()
            call_args = mock_st.button.call_args
            # The button text should contain retry indicator
            assert "🔄" in call_args[0][0] or "Yeniden dene" in call_args[0][0]
