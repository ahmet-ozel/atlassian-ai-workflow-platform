"""Loop Guard Account ID Resolution.



Background
----------

After account probing at startup, post-create, and wizard flows, all
departments have their ``bot.<service>.account_id`` fields populated.
The loop guard operates in two tiers:

1. **First tier (account_id match)** - when ``actor_account_id`` is
 present on the webhook event, the guard checks membership in the
 union of all bot account IDs. A hit drops the event with reason
 ``loop_guard_dropped``.

2. **Regex fallback** - when ``actor_account_id`` is ``None`` (legacy
 payloads from older Atlassian delivery shapes), the guard scans
 ``body_text`` for the ``^\\s*\\[bot:`` prefix. A hit drops with
 reason ``loop_guard_regex_dropped``.

This invariant verifies:

(a) When all departments have account_id filled (the normal
 steady state), the loop guard uses first-tier account_id matching
 for bot-authored events - the regex fallback is never reached.

(b) The regex fallback activates **only** for legacy payloads where
 ``actor_account_id`` is ``None`` (missing from the payload).

(c) A human actor quoting ``[bot:`` text is never dropped by the
 regex fallback because the first-tier path (actor present, not in
 registry) short-circuits to pass.

Strategy
--------

We use Hypothesis to generate random department configurations where
every department has a non-empty ``account_id`` (simulating the
steady state). We then construct webhook events with various
actor/body combinations and verify the loop guard's two-tier decision
logic through the real ``WebhookFilterChain.evaluate`` method.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap - expose the automation-service source root
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_AUTOMATION_ROOT: Final[Path] = (
    _PLATFORM_ROOT / "services" / "automation-service"
)
_AUTOMATION_SRC: Final[Path] = _AUTOMATION_ROOT / "src"

for _p in (_AUTOMATION_ROOT, _AUTOMATION_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

# Also add libs that the module imports from
_LIB_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "db-shared" / "src",
    _PLATFORM_ROOT / "libs" / "vault_client" / "src",
)
for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


from automation_service.webhook_filters import (  # noqa: E402
    BOT_PREFIX_REGEX,
    REASON_FILTER_CHAIN_PASS,
    REASON_LOOP_GUARD_DROPPED,
    REASON_LOOP_GUARD_REGEX_DROPPED,
    FilterDecision,
    WebhookEvent,
    WebhookFilterChain,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Account ID strategy - hex-like strings mimicking Atlassian account IDs.
#: These represent the steady state where every dept has a
#: resolved account_id.
_account_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_:"
    ),
    min_size=8,
    max_size=32,
)

#: Bot registry strategy - non-empty frozensets representing the union
#: of all department bot account IDs after account probing has
#: filled every slot.
_filled_bot_registries = st.frozensets(_account_ids, min_size=1, max_size=8)

#: Human actor IDs - guaranteed not to be in the bot registry.
_human_actor_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_:"
    ),
    min_size=8,
    max_size=32,
)

#: Body text that matches the ``[bot:`` regex (legacy bot comment format).
_bot_prefix_bodies = st.builds(
    lambda ws, suffix: f"{ws}[bot:{suffix}]",
    ws=st.sampled_from(["", " ", " ", "\t", " \t "]),
    suffix=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cc",), blacklist_characters="\n\r"
        ),
        min_size=0,
        max_size=20,
    ),
)

#: Body text that does NOT match the ``[bot:`` regex.
_non_bot_bodies = st.one_of(
    st.text(min_size=0, max_size=40).filter(
        lambda s: BOT_PREFIX_REGEX.search(s) is None
    ),
    st.none(),
)


# ---------------------------------------------------------------------------
# Helper: build a WebhookFilterChain with minimal collaborators
# ---------------------------------------------------------------------------


def _build_chain(
    *,
    bot_account_ids: frozenset[str],
) -> WebhookFilterChain:
    """Construct a chain whose non-loop-guard stages are no-op pass-through."""

    return WebhookFilterChain(
        verify_hmac=lambda ev: True,
        resolve_dept=lambda ev: "test-dept",
        bot_account_ids=lambda: bot_account_ids,
        is_processed=lambda d: False,
        mention_set_for=lambda i: frozenset(),
        iter_count_for=lambda i: 0,
        reporter_for=lambda i: "reporter-1",
    )


def _make_event(
    *,
    actor_account_id: str | None = "human-user",
    body_text: str | None = None,
) -> WebhookEvent:
    """Construct a WebhookEvent with property-test-friendly defaults."""

    return WebhookEvent(
        provider="jira",
        event_type="jira:issue_created",
        delivery_id="delivery-test-1",
        actor_account_id=actor_account_id,
        body_text=body_text,
        project_key="PAY",
        repo_slug=None,
        issue_key="PAY-1",
        pr_id=None,
    )


# ---------------------------------------------------------------------------
# invariant: all depts have account_id filled,
# loop guard uses first-tier (account_id match) for bot events
# ---------------------------------------------------------------------------


class TestLoopGuardFirstTierResolution:
    """Exercise account-id based detection for bot self-action.

 The loop guard's first tier (actor_account_id membership check) is
 the active path for detecting bot self-action when department bot
 account_ids are filled. The regex fallback is never reached because
 the actor field is always present in modern payloads.
 """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_filled_bot_registries,
        data=st.data(),
    )
    def test_bot_actor_drops_via_account_id_match(
        self, bot_ids: frozenset[str], data: st.DataObject
    ) -> None:
        """Bot actors are dropped by account-id matching.

 When all depts have account_id filled and the webhook event carries
 a bot's account_id as the actor, the loop guard drops the event
 using first-tier account_id matching - reason is
 ``loop_guard_dropped``, NOT ``loop_guard_regex_dropped``.
 """

        # Pick any bot account_id from the filled registry
        bot_actor = data.draw(st.sampled_from(sorted(bot_ids)))

        chain = _build_chain(bot_account_ids=bot_ids)

        # Even if body_text matches the [bot: regex, the first tier
        # should fire (account_id match takes precedence)
        body = data.draw(st.one_of(_bot_prefix_bodies, _non_bot_bodies))
        event = _make_event(actor_account_id=bot_actor, body_text=body)

        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "drop"
        # First tier fires - NOT the regex fallback
        assert decision.reason == REASON_LOOP_GUARD_DROPPED

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_filled_bot_registries,
        human_actor=_human_actor_ids,
        body=_non_bot_bodies,
    )
    def test_human_actor_passes_first_tier(
        self, bot_ids: frozenset[str], human_actor: str, body: str | None
    ) -> None:
        """Human actors pass the account-id check.

 A human actor (not in the bot registry) passes the first-tier check.
 With a non-bot body, the event flows through the entire chain without
 being dropped.
 """

        assume(human_actor not in bot_ids)

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=human_actor, body_text=body)

        decision = chain.evaluate(event)

        assert decision.action == "pass"

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_filled_bot_registries,
        human_actor=_human_actor_ids,
        body=_bot_prefix_bodies,
    )
    def test_human_actor_with_bot_quote_not_dropped(
        self, bot_ids: frozenset[str], human_actor: str, body: str
    ) -> None:
        """Human comments quoting bot-looking text are not dropped.

 A human actor quoting ``[bot:`` text in their comment is NOT
 dropped by the regex fallback. The first-tier path (actor
 present, not in registry) short-circuits to pass - the regex
 fallback is gated on ``actor_account_id is None``.

 This confirms that when actors have account_ids, the regex path is
 unreachable for legitimate human comments.
 """

        assume(human_actor not in bot_ids)

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=human_actor, body_text=body)

        decision = chain.evaluate(event)

        # Human actor passes even with [bot: in body
        assert decision.action == "pass"


# ---------------------------------------------------------------------------
# invariant: Regex fallback activates ONLY for legacy payloads
# (actor_account_id is None)
# ---------------------------------------------------------------------------


class TestLoopGuardRegexFallbackLegacyOnly:
    """Exercise the legacy regex fallback gate.

 The regex fallback (``^\\s*\\[bot:``) activates ONLY when
 ``actor_account_id`` is ``None`` - the legacy payload shape where
 Atlassian's older delivery format omits the user attribution.
 This is the only scenario where the regex path is reachable.
 """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        bot_ids=_filled_bot_registries,
        body=_bot_prefix_bodies,
    )
    def test_legacy_payload_with_bot_prefix_drops_via_regex(
        self, bot_ids: frozenset[str], body: str
    ) -> None:
        """Legacy bot-prefix payloads are dropped by regex.

 Legacy payloads (``actor_account_id=None``) with body text
 matching ``^\\s*\\[bot:`` are dropped via the regex fallback.
 The reason is ``loop_guard_regex_dropped`` - distinct from
 the first-tier ``loop_guard_dropped``.
 """

        # Sanity: the generated body matches the regex
        assert BOT_PREFIX_REGEX.search(body) is not None

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=None, body_text=body)

        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_REGEX_DROPPED

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        bot_ids=_filled_bot_registries,
        body=_non_bot_bodies,
    )
    def test_legacy_payload_without_bot_prefix_passes(
        self, bot_ids: frozenset[str], body: str | None
    ) -> None:
        """Legacy payloads without a bot prefix pass through.

 Legacy payloads (``actor_account_id=None``) whose body does
 NOT match the ``[bot:`` regex pass through - they are treated
 as system events with no bot footprint.
 """

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=None, body_text=body)

        decision = chain.evaluate(event)

        assert decision.action == "pass"

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_filled_bot_registries,
        actor=_account_ids,
        body=_bot_prefix_bodies,
    )
    def test_regex_fallback_never_fires_when_actor_present(
        self, bot_ids: frozenset[str], actor: str, body: str
    ) -> None:
        """Present actors never use the regex fallback.

 When ``actor_account_id`` is present (non-None), the regex
 fallback NEVER fires - regardless of body content. The
 decision is either ``loop_guard_dropped`` (actor in registry)
 or ``pass`` (actor not in registry). The reason is never
 ``loop_guard_regex_dropped``.

 This is the key invariant for modern payloads that carry an actor:
 the regex path is reserved exclusively for legacy compatibility.
 """

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=actor, body_text=body)

        decision = chain.evaluate(event)

        # The regex reason must NEVER appear when actor is present
        assert decision.reason != REASON_LOOP_GUARD_REGEX_DROPPED

        # Decision is either first-tier drop or pass
        if actor in bot_ids:
            assert decision.action == "drop"
            assert decision.reason == REASON_LOOP_GUARD_DROPPED
        else:
            assert decision.action == "pass"


# ---------------------------------------------------------------------------
# invariant: First-tier dominance - account_id match always takes
# precedence over regex when both could fire
# ---------------------------------------------------------------------------


class TestFirstTierDominatesRegex:
    """Account-id matches take precedence over regex matches.

 When a bot's account_id is present AND the body matches the
 ``[bot:`` regex, the first-tier (account_id match) fires and
 produces ``loop_guard_dropped``. The regex fallback is never
 consulted because the first-tier short-circuits on actor presence.
 """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_filled_bot_registries,
        body=_bot_prefix_bodies,
        data=st.data(),
    )
    def test_account_id_match_dominates_regex_match(
        self, bot_ids: frozenset[str], body: str, data: st.DataObject
    ) -> None:
        """Bot account-id matches win when both signals are present.

 Both conditions are true: actor is a bot AND body matches
 ``[bot:``. The chain produces ``loop_guard_dropped`` (first
 tier), NOT ``loop_guard_regex_dropped`` (fallback).
 """

        bot_actor = data.draw(st.sampled_from(sorted(bot_ids)))

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=bot_actor, body_text=body)

        decision = chain.evaluate(event)

        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_DROPPED

    @settings(max_examples=100, deadline=2000)
    @given(bot_ids=_filled_bot_registries)
    def test_empty_body_with_bot_actor_still_drops_first_tier(
        self, bot_ids: frozenset[str]
    ) -> None:
        """Bot actors are still dropped when the body is empty.

 A bot actor with no body text (None) is still caught by the
 first tier. The regex fallback cannot fire because (a) actor
 is present and (b) body is None.
 """

        bot_actor = sorted(bot_ids)[0]

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=bot_actor, body_text=None)

        decision = chain.evaluate(event)

        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_DROPPED


# ---------------------------------------------------------------------------
# invariant: steady state - all depts filled means
# the regex path is only reachable via legacy (None actor) payloads
# ---------------------------------------------------------------------------


class TestPostProbeInvariant:
    """Exercise the filled-registry steady state.

 In the steady state, the bot registry is non-empty
 (every dept has at least one bot account_id). The regex fallback
 path is reachable ONLY when ``actor_account_id is None`` - which
 corresponds to legacy Atlassian delivery shapes that omit user
 attribution.
 """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_filled_bot_registries,
        actor=st.one_of(st.none(), _account_ids),
        body=st.one_of(_bot_prefix_bodies, _non_bot_bodies),
    )
    def test_regex_reason_implies_none_actor(
        self, bot_ids: frozenset[str], actor: str | None, body: str | None
    ) -> None:
        """Regex drops imply a missing actor.

 For any combination of actor and body, if the chain returns
 ``loop_guard_regex_dropped``, then ``actor_account_id`` MUST
 be ``None``. This is the fundamental invariant that ensures
 the regex fallback is reserved for legacy payloads only.
 """

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=actor, body_text=body)

        decision = chain.evaluate(event)

        if decision.reason == REASON_LOOP_GUARD_REGEX_DROPPED:
            # The regex fallback fired - actor MUST be None (legacy)
            assert actor is None, (
                f"Regex fallback fired with non-None actor={actor!r}; "
                f"this violates the the operational rule invariant that regex is "
                f"reserved for legacy payloads only."
            )

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_filled_bot_registries,
        actor=_account_ids,
        body=st.one_of(_bot_prefix_bodies, _non_bot_bodies),
    )
    def test_modern_payload_never_uses_regex_path(
        self, bot_ids: frozenset[str], actor: str, body: str | None
    ) -> None:
        """Modern payloads never use the regex path.

 Modern payloads (actor_account_id is always present) NEVER produce
 the ``loop_guard_regex_dropped``
 reason. The decision is always either ``loop_guard_dropped``
 (bot actor) or a pass-through reason.
 """

        chain = _build_chain(bot_account_ids=bot_ids)
        event = _make_event(actor_account_id=actor, body_text=body)

        decision = chain.evaluate(event)

        assert decision.reason != REASON_LOOP_GUARD_REGEX_DROPPED
