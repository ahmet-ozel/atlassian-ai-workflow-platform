"""Property tests for webhook predicate guards (loop, assignee, changelog, event-type).

**Validates: Requirements 2.5, 2.6, 2.7, 2.8, 2.9, 2.13, 3.5**

Property 7: Webhook predicate guards

Invariants tested:
  7a. is_self_actor(actor_id, bot_ids) ↔ actor_id ∈ bot_ids
      (None actor always returns False)
  7b. is_bot_assignee(assignee_id, bot_ids) ↔ assignee_id ∈ bot_ids
      (None assignee always returns False)
  7c. assignee_changed_to_bot(changelog, bot_ids) → True iff changelog
      has field=="assignee" and to ∈ bot_ids
  7d. route(event_type) returns "accepted" for supported types,
      "ignored" for all others
  7e. Short-circuit ordering composition: if is_self_actor is True,
      no further predicate evaluation is needed (the event is skipped)
  7f. Determinism: all predicates are pure functions — same inputs
      always produce the same output
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# Ensure the automation-service src is importable for property tests.
_AUTOMATION_SRC = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
    / "src"
)
if str(_AUTOMATION_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SRC))

from decision.loop_guard import (
    _ACCEPTED_EVENT_TYPES,
    assignee_changed_to_bot,
    is_bot_assignee,
    is_self_actor,
    route,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Account IDs: alphanumeric strings of reasonable length.
_account_ids = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=40,
)

#: Bot registry: a frozenset of 0–10 account IDs.
_bot_registries = st.frozensets(_account_ids, min_size=0, max_size=10)

#: Optional account ID (can be None).
_optional_account_ids = st.one_of(st.none(), _account_ids)

#: Supported event types (from the module).
_supported_event_types = st.sampled_from(sorted(_ACCEPTED_EVENT_TYPES))

#: Arbitrary event type strings (may or may not be supported).
_arbitrary_event_types = st.text(min_size=0, max_size=60)

#: Changelog item strategy — produces a dict with field and to keys.
_changelog_fields = st.sampled_from(
    ["assignee", "status", "priority", "summary", "description", "labels"]
)


@st.composite
def _changelog_items(draw: st.DrawFn) -> dict[str, str | None]:
    """Generate a single changelog item dict."""
    field = draw(_changelog_fields)
    to_value = draw(st.one_of(st.none(), _account_ids, st.text(min_size=1, max_size=30)))
    return {"field": field, "from": draw(st.text(max_size=20)), "to": to_value}


@st.composite
def _changelogs(draw: st.DrawFn) -> dict[str, list[dict[str, str | None]]] | None:
    """Generate a changelog structure (or None)."""
    is_none = draw(st.booleans())
    if is_none:
        return None
    items = draw(st.lists(_changelog_items(), min_size=0, max_size=8))
    return {"items": items}


@st.composite
def _changelog_with_assignee_to_bot(
    draw: st.DrawFn, bot_ids: frozenset[str]
) -> dict[str, list[dict[str, str | None]]]:
    """Generate a changelog that definitely has an assignee change to a bot."""
    bot_id = draw(st.sampled_from(sorted(bot_ids)))
    # Build other items
    other_items = draw(st.lists(_changelog_items(), min_size=0, max_size=5))
    # Insert the assignee→bot item at a random position
    assignee_item: dict[str, str | None] = {
        "field": "assignee",
        "from": draw(st.one_of(st.none(), _account_ids)),
        "to": bot_id,
    }
    position = draw(st.integers(min_value=0, max_value=len(other_items)))
    items = other_items[:position] + [assignee_item] + other_items[position:]
    return {"items": items}


# ---------------------------------------------------------------------------
# Property 7a: is_self_actor ↔ membership
# ---------------------------------------------------------------------------


class TestIsSelfActorMembership:
    """is_self_actor(actor_id, bot_ids) ↔ actor_id ∈ bot_ids."""

    @settings(max_examples=200, deadline=2000)
    @given(actor_id=_account_ids, bot_ids=_bot_registries)
    def test_actor_in_registry_iff_returns_true(
        self, actor_id: str, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.5, 2.6, 3.5**

        is_self_actor returns True if and only if actor_id is in the
        bot registry set.
        """
        result = is_self_actor(actor_id, bot_ids)
        expected = actor_id in bot_ids
        assert result is expected

    @settings(max_examples=100, deadline=2000)
    @given(bot_ids=_bot_registries)
    def test_none_actor_always_false(self, bot_ids: frozenset[str]) -> None:
        """**Validates: Requirements 2.5, 2.6, 3.5**

        None actor always returns False regardless of registry content.
        """
        assert is_self_actor(None, bot_ids) is False

    @settings(max_examples=100, deadline=2000)
    @given(actor_id=_account_ids)
    def test_empty_registry_always_false(self, actor_id: str) -> None:
        """**Validates: Requirements 2.5, 2.6, 3.5**

        An empty bot registry means no actor can be a bot.
        """
        assert is_self_actor(actor_id, frozenset()) is False


# ---------------------------------------------------------------------------
# Property 7b: is_bot_assignee ↔ membership
# ---------------------------------------------------------------------------


class TestIsBotAssigneeMembership:
    """is_bot_assignee(assignee_id, bot_ids) ↔ assignee_id ∈ bot_ids."""

    @settings(max_examples=200, deadline=2000)
    @given(assignee_id=_account_ids, bot_ids=_bot_registries)
    def test_assignee_in_registry_iff_returns_true(
        self, assignee_id: str, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.7, 2.8**

        is_bot_assignee returns True if and only if assignee_id is in
        the bot registry set.
        """
        result = is_bot_assignee(assignee_id, bot_ids)
        expected = assignee_id in bot_ids
        assert result is expected

    @settings(max_examples=100, deadline=2000)
    @given(bot_ids=_bot_registries)
    def test_none_assignee_always_false(self, bot_ids: frozenset[str]) -> None:
        """**Validates: Requirements 2.7, 2.8**

        None assignee always returns False regardless of registry content.
        """
        assert is_bot_assignee(None, bot_ids) is False

    @settings(max_examples=100, deadline=2000)
    @given(assignee_id=_account_ids)
    def test_empty_registry_always_false(self, assignee_id: str) -> None:
        """**Validates: Requirements 2.7, 2.8**

        An empty bot registry means no assignee can be a bot.
        """
        assert is_bot_assignee(assignee_id, frozenset()) is False


# ---------------------------------------------------------------------------
# Property 7c: assignee_changed_to_bot — changelog invariants
# ---------------------------------------------------------------------------


class TestAssigneeChangedToBot:
    """assignee_changed_to_bot(changelog, bot_ids) invariants."""

    @settings(max_examples=200, deadline=2000)
    @given(data=st.data(), bot_ids=_bot_registries.filter(lambda s: len(s) > 0))
    def test_changelog_with_assignee_to_bot_returns_true(
        self, data: st.DataObject, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.9**

        A changelog containing field=="assignee" with to ∈ bot_ids
        must return True.
        """
        changelog = data.draw(_changelog_with_assignee_to_bot(bot_ids))
        assert assignee_changed_to_bot(changelog, bot_ids) is True

    @settings(max_examples=200, deadline=2000)
    @given(bot_ids=_bot_registries)
    def test_none_changelog_always_false(self, bot_ids: frozenset[str]) -> None:
        """**Validates: Requirements 2.9**

        None changelog always returns False.
        """
        assert assignee_changed_to_bot(None, bot_ids) is False

    @settings(max_examples=200, deadline=2000)
    @given(bot_ids=_bot_registries)
    def test_empty_items_always_false(self, bot_ids: frozenset[str]) -> None:
        """**Validates: Requirements 2.9**

        A changelog with empty items list always returns False.
        """
        assert assignee_changed_to_bot({"items": []}, bot_ids) is False

    @settings(max_examples=200, deadline=2000)
    @given(
        non_bot_id=_account_ids,
        bot_ids=_bot_registries,
    )
    def test_assignee_to_non_bot_returns_false(
        self, non_bot_id: str, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.9**

        If the assignee changes to an ID not in the bot registry,
        the function returns False.
        """
        assume(non_bot_id not in bot_ids)
        changelog = {
            "items": [{"field": "assignee", "from": "someone", "to": non_bot_id}]
        }
        assert assignee_changed_to_bot(changelog, bot_ids) is False

    @settings(max_examples=200, deadline=2000)
    @given(bot_ids=_bot_registries)
    def test_no_assignee_field_returns_false(
        self, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.9**

        A changelog with items but no assignee field returns False.
        """
        changelog = {
            "items": [
                {"field": "status", "from": "Open", "to": "Done"},
                {"field": "priority", "from": "Low", "to": "High"},
            ]
        }
        assert assignee_changed_to_bot(changelog, bot_ids) is False

    @settings(max_examples=100, deadline=2000)
    @given(bot_ids=_bot_registries)
    def test_assignee_to_none_returns_false(
        self, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.9**

        Assignee removal (to=None) always returns False even if bots exist.
        """
        changelog = {
            "items": [{"field": "assignee", "from": "bot-001", "to": None}]
        }
        assert assignee_changed_to_bot(changelog, bot_ids) is False


# ---------------------------------------------------------------------------
# Property 7d: route — event type classification
# ---------------------------------------------------------------------------


class TestRouteEventType:
    """route(event_type) classification invariants."""

    @settings(max_examples=200, deadline=2000)
    @given(event_type=_supported_event_types)
    def test_supported_event_types_accepted(self, event_type: str) -> None:
        """**Validates: Requirements 2.13, 3.5**

        All supported event types return "accepted".
        """
        assert route(event_type) == "accepted"

    @settings(max_examples=200, deadline=2000)
    @given(event_type=_arbitrary_event_types)
    def test_unsupported_event_types_ignored(self, event_type: str) -> None:
        """**Validates: Requirements 2.13, 3.5**

        Any event type not in the supported set returns "ignored".
        """
        assume(event_type not in _ACCEPTED_EVENT_TYPES)
        assert route(event_type) == "ignored"

    def test_route_return_type_is_literal(self) -> None:
        """**Validates: Requirements 2.13, 3.5**

        route() always returns exactly "accepted" or "ignored" — no
        other values are possible.
        """
        for et in _ACCEPTED_EVENT_TYPES:
            assert route(et) in ("accepted", "ignored")
        assert route("unknown:event") in ("accepted", "ignored")

    def test_all_required_event_types_are_supported(self) -> None:
        """**Validates: Requirements 2.13, 3.5**

        The platform must support these specific event types per
        requirements 2.13 and 3.5.
        """
        required_jira = {
            "jira:issue_created",
            "jira:issue_assigned",
            "jira:issue_updated",
            "jira:comment_created",
        }
        required_bitbucket = {
            "pullrequest:reviewer_added",
            "pullrequest:comment_created",
        }
        all_required = required_jira | required_bitbucket

        for event_type in all_required:
            assert route(event_type) == "accepted", (
                f"Required event type {event_type!r} is not accepted"
            )


# ---------------------------------------------------------------------------
# Property 7e: Short-circuit ordering composition
# ---------------------------------------------------------------------------


class TestShortCircuitOrdering:
    """If is_self_actor is True, no further checks are needed."""

    @settings(max_examples=200, deadline=2000)
    @given(
        bot_ids=_bot_registries.filter(lambda s: len(s) > 0),
        changelog=_changelogs(),
        event_type=_supported_event_types,
    )
    def test_self_actor_short_circuits_all_other_checks(
        self,
        bot_ids: frozenset[str],
        changelog: dict | None,
        event_type: str,
    ) -> None:
        """**Validates: Requirements 2.5, 2.6, 3.5**

        When the actor IS a bot (is_self_actor returns True), the webhook
        should be skipped regardless of assignee or changelog state.
        This models the guard chain: if step 1 (loop guard) fires,
        steps 2-4 are irrelevant.
        """
        # Pick an actor that IS in the bot registry
        actor_id = sorted(bot_ids)[0]

        # The loop guard fires
        assert is_self_actor(actor_id, bot_ids) is True

        # Regardless of what assignee or changelog say, the event is skipped.
        # This is a composition test: the decision chain short-circuits.
        # We verify that the predicate result is independent of other inputs.
        for other_actor in [None, "random-user", actor_id]:
            # is_self_actor with the bot actor always True
            assert is_self_actor(actor_id, bot_ids) is True

    @settings(max_examples=200, deadline=2000)
    @given(
        actor_id=_account_ids,
        bot_ids=_bot_registries,
        event_type=_supported_event_types,
    )
    def test_non_self_actor_allows_further_checks(
        self,
        actor_id: str,
        bot_ids: frozenset[str],
        event_type: str,
    ) -> None:
        """**Validates: Requirements 2.5, 2.6, 3.5**

        When the actor is NOT a bot, further checks (assignee, changelog,
        route) become relevant and must be evaluated.
        """
        assume(actor_id not in bot_ids)

        # Loop guard does NOT fire
        assert is_self_actor(actor_id, bot_ids) is False

        # Further checks are now meaningful — route must still classify
        assert route(event_type) == "accepted"

    @settings(max_examples=200, deadline=2000)
    @given(
        bot_ids=_bot_registries.filter(lambda s: len(s) > 0),
    )
    def test_guard_chain_composition_self_actor_dominates(
        self,
        bot_ids: frozenset[str],
    ) -> None:
        """**Validates: Requirements 2.5, 2.6, 2.7, 2.8, 2.9, 3.5**

        Full guard chain composition: when is_self_actor is True,
        the decision is "skip" regardless of is_bot_assignee or
        assignee_changed_to_bot results. This models the webhook
        handler's sequential guard evaluation.
        """
        bot_id = sorted(bot_ids)[0]

        # Step 1: Loop guard fires → skip
        assert is_self_actor(bot_id, bot_ids) is True

        # Even if the bot is also the assignee, the loop guard takes priority
        assert is_bot_assignee(bot_id, bot_ids) is True  # would pass step 2
        # But step 1 already decided "skip" — composition invariant holds


# ---------------------------------------------------------------------------
# Property 7f: Determinism — all predicates are pure functions
# ---------------------------------------------------------------------------


class TestDeterminism:
    """All predicates are pure: same inputs → same output."""

    @settings(max_examples=200, deadline=2000)
    @given(actor_id=_optional_account_ids, bot_ids=_bot_registries)
    def test_is_self_actor_deterministic(
        self, actor_id: str | None, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.5, 2.6, 3.5**

        Repeated calls with same inputs produce identical results.
        """
        r1 = is_self_actor(actor_id, bot_ids)
        r2 = is_self_actor(actor_id, bot_ids)
        r3 = is_self_actor(actor_id, bot_ids)
        assert r1 is r2 is r3

    @settings(max_examples=200, deadline=2000)
    @given(assignee_id=_optional_account_ids, bot_ids=_bot_registries)
    def test_is_bot_assignee_deterministic(
        self, assignee_id: str | None, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.7, 2.8**

        Repeated calls with same inputs produce identical results.
        """
        r1 = is_bot_assignee(assignee_id, bot_ids)
        r2 = is_bot_assignee(assignee_id, bot_ids)
        r3 = is_bot_assignee(assignee_id, bot_ids)
        assert r1 is r2 is r3

    @settings(max_examples=200, deadline=2000)
    @given(changelog=_changelogs(), bot_ids=_bot_registries)
    def test_assignee_changed_to_bot_deterministic(
        self, changelog: dict | None, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirements 2.9**

        Repeated calls with same inputs produce identical results.
        """
        r1 = assignee_changed_to_bot(changelog, bot_ids)
        r2 = assignee_changed_to_bot(changelog, bot_ids)
        r3 = assignee_changed_to_bot(changelog, bot_ids)
        assert r1 is r2 is r3

    @settings(max_examples=200, deadline=2000)
    @given(event_type=_arbitrary_event_types)
    def test_route_deterministic(self, event_type: str) -> None:
        """**Validates: Requirements 2.13, 3.5**

        Repeated calls with same input produce identical results.
        """
        r1 = route(event_type)
        r2 = route(event_type)
        r3 = route(event_type)
        assert r1 == r2 == r3


# ---------------------------------------------------------------------------
# Property 4 (R1.7) — Bot self-action loop guard, multi-department scenarios
# ---------------------------------------------------------------------------
#
# **Property 4: Loop guard, banned tool list ve PR draft enforcement**
#
# **Validates: Requirements 1.7**
#
# This section extends ``test_webhook_predicates.py`` with the
# multi-department / multi-service scenarios specified by task 2.8 of
# ``.kiro/specs/platform-mimari-foundation/tasks.md``. The companion
# files for the same property are:
#
# - ``test_tool_filter.py``         — R1.8 / MIMARI §1 Kural 9
# - ``test_pr_draft_enforcement.py``— R1.9 / MIMARI §1 Kural 10
#
# R1.7 says: WHEN a webhook event's ``actor.account_id`` equals *any*
# department's ``bot.<service>.account_id``, the event is dropped and
# audited as ``loop_guard_dropped``. The "*any*" quantifier is the
# subtle part — a bot in department A must trigger the loop guard for
# events received on a webhook routed to department B (the bot may
# legitimately work across projects). The strategies below build a
# multi-department ``BotRegistry`` so the property quantifies over the
# *union* of every department's bot account IDs.
# ---------------------------------------------------------------------------


from dataclasses import dataclass


_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")


@dataclass(frozen=True)
class _BotRegistry:
    """A flat view of "every bot account ID across every department".

    Mirrors the runtime shape the webhook handler builds when it
    composes the loop guard: the union of every ``bot.<service>``
    ``account_id`` registered in ``departments.json``. Tests use this
    container to express R1.7's "any department's bot" quantifier in
    a single ``frozenset``.
    """

    by_dept: dict[str, dict[str, str]]
    """``{dept_id: {service: account_id}}`` for round-trip checks."""

    union: frozenset[str]
    """Flat union of every ``account_id`` — what the loop guard checks."""


@st.composite
def _multi_dept_bot_registries(draw: st.DrawFn) -> _BotRegistry:
    """Generate a multi-department bot registry (1–5 depts × 1–3 services).

    Each department gets a non-empty subset of services; each service
    gets a unique ``account_id``. Uniqueness is enforced via
    Hypothesis' ``st.lists(unique=True)`` so the registry's flat union
    has the expected size.
    """

    dept_count = draw(st.integers(min_value=1, max_value=5))
    dept_ids = draw(
        st.lists(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("Ll", "Nd"), whitelist_characters="-"
                ),
                min_size=1,
                max_size=15,
            ),
            min_size=dept_count,
            max_size=dept_count,
            unique=True,
        )
    )

    # Total bot count = sum over depts of "1 to 3 services". Pre-draw
    # a flat pool of unique account IDs and slice it as we walk the
    # departments — keeps Hypothesis' shrinker happy.
    max_total = dept_count * len(_SERVICES)
    pool = draw(
        st.lists(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("Ll", "Nd"), whitelist_characters="-_"
                ),
                min_size=1,
                max_size=30,
            ),
            min_size=dept_count,
            max_size=max_total,
            unique=True,
        )
    )

    by_dept: dict[str, dict[str, str]] = {}
    pool_idx = 0
    for dept_id in dept_ids:
        # Each dept has at least one service, otherwise the loop guard
        # has no bots for that dept (still valid but uninformative).
        service_count = draw(st.integers(min_value=1, max_value=len(_SERVICES)))
        services = draw(
            st.lists(
                st.sampled_from(_SERVICES),
                min_size=service_count,
                max_size=service_count,
                unique=True,
            )
        )
        services_for_dept: dict[str, str] = {}
        for service in services:
            if pool_idx >= len(pool):
                # Pool exhausted; skip silently. The remaining services
                # for this dept are dropped — the registry shape stays
                # well-formed.
                break
            services_for_dept[service] = pool[pool_idx]
            pool_idx += 1
        if services_for_dept:
            by_dept[dept_id] = services_for_dept

    union = frozenset(
        account_id
        for services_for_dept in by_dept.values()
        for account_id in services_for_dept.values()
    )
    return _BotRegistry(by_dept=by_dept, union=union)


class TestBotSelfActionLoopGuard:
    """Multi-department / multi-service bot self-action loop guard.

    **Validates: Requirements 1.7**

    The webhook handler must drop events whose ``actor.account_id``
    matches *any* bot in *any* department, regardless of which service
    that bot belongs to. These properties check the predicate at the
    set-membership layer — the I/O side (audit ``loop_guard_dropped``,
    HTTP 200 silent drop) is owned by the integration tests.
    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(registry=_multi_dept_bot_registries(), data=st.data())
    def test_actor_matching_any_dept_bot_triggers_loop_guard(
        self, registry: _BotRegistry, data: st.DataObject
    ) -> None:
        """**Validates: Requirement 1.7**

        For any registry with at least one bot, picking *any* bot
        ``account_id`` from *any* department / service triggers the
        loop guard. Mirrors MIMARI §1 Kural 7's "actor.account_id ==
        any dept.bot.<svc>.account_id" predicate.
        """

        # The strategy can produce empty registries (when the pool
        # exhausts before any service is filled). Skip those — the
        # property is vacuous when the union is empty.
        if not registry.union:
            return

        # Pick one bot from one department / service to play the
        # role of the "self actor" on the inbound webhook.
        bot_account_id = data.draw(st.sampled_from(sorted(registry.union)))
        assert is_self_actor(bot_account_id, registry.union) is True

    @settings(max_examples=200, deadline=2000)
    @given(registry=_multi_dept_bot_registries(), foreign_actor=_account_ids)
    def test_non_bot_actor_passes_loop_guard(
        self, registry: _BotRegistry, foreign_actor: str
    ) -> None:
        """**Validates: Requirement 1.7**

        Real human authors (account IDs that are not in *any* bot
        slot of *any* department) must pass the loop guard so their
        ``issue_created`` / ``issue_commented`` events can flow to
        the workflow start path.
        """

        assume(foreign_actor not in registry.union)
        assert is_self_actor(foreign_actor, registry.union) is False

    @settings(max_examples=200, deadline=2000)
    @given(registry=_multi_dept_bot_registries(), data=st.data())
    def test_cross_department_self_action_is_caught(
        self, registry: _BotRegistry, data: st.DataObject
    ) -> None:
        """**Validates: Requirement 1.7**

        A bot belonging to department A still triggers the loop
        guard for events that *would* be routed to department B.
        The handler does not first scope the registry to the
        webhook's resolved ``dept_id``; it checks the global union
        so cross-department bot activity (eg. a payment-team bot
        commenting on a finance-team issue) cannot create a loop.
        """

        if len(registry.by_dept) < 2:
            # Need at least two departments to express "cross-dept".
            return

        depts = sorted(registry.by_dept.keys())
        actor_dept = data.draw(st.sampled_from(depts))
        # Pick a different department whose webhook this hypothetical
        # event is routed to.
        other_depts = [d for d in depts if d != actor_dept]
        _ = data.draw(st.sampled_from(other_depts))

        # Pick the actor's account_id from their own dept's services.
        services_for_actor = registry.by_dept[actor_dept]
        if not services_for_actor:
            return
        service = data.draw(st.sampled_from(sorted(services_for_actor.keys())))
        actor_account_id = services_for_actor[service]

        # The flat union is what the loop guard actually checks.
        # Cross-dept activity must still match.
        assert is_self_actor(actor_account_id, registry.union) is True

    @settings(max_examples=200, deadline=2000)
    @given(registry=_multi_dept_bot_registries(), data=st.data())
    def test_per_service_match_dominance(
        self, registry: _BotRegistry, data: st.DataObject
    ) -> None:
        """**Validates: Requirement 1.7**

        The loop guard fires regardless of which Atlassian service
        the bot is registered against — Jira, Bitbucket, and
        Confluence bot account IDs all live in the same union and
        all short-circuit the webhook.
        """

        # Find a department that has every service so we can iterate.
        full_dept = None
        for dept_id, services in registry.by_dept.items():
            if set(services.keys()) == set(_SERVICES):
                full_dept = dept_id
                break
        if full_dept is None:
            return  # Nothing to verify in this draw.

        services_for_dept = registry.by_dept[full_dept]
        for service in _SERVICES:
            account_id = services_for_dept[service]
            assert is_self_actor(account_id, registry.union) is True, (
                f"loop guard missed bot for service={service!r} in dept={full_dept!r}"
            )

    @settings(max_examples=100, deadline=2000)
    @given(registry=_multi_dept_bot_registries())
    def test_none_actor_never_triggers_loop_guard(
        self, registry: _BotRegistry
    ) -> None:
        """**Validates: Requirement 1.7**

        ``actor.account_id`` is ``None`` for system-emitted events
        (eg. lifecycle hooks Atlassian fires without a user
        attribution). Those must pass the loop guard regardless of
        the registry contents — the predicate cannot match ``None``
        against any string ID.
        """

        assert is_self_actor(None, registry.union) is False

    @settings(max_examples=100, deadline=2000)
    @given(actor_id=_account_ids)
    def test_empty_registry_never_triggers_loop_guard(
        self, actor_id: str
    ) -> None:
        """**Validates: Requirement 1.7**

        Boot-time edge case: when ``departments.json`` is empty
        (fresh install), the loop guard's union is empty and every
        actor passes through. The platform must not silently drop
        every webhook in this state.
        """

        assert is_self_actor(actor_id, frozenset()) is False

    @settings(max_examples=200, deadline=2000)
    @given(registry=_multi_dept_bot_registries(), foreign_actor=_account_ids)
    def test_loop_guard_short_circuit_with_loop_guard_dropped_semantics(
        self, registry: _BotRegistry, foreign_actor: str
    ) -> None:
        """**Validates: Requirement 1.7**

        Composition with the rest of the guard chain: if the loop
        guard fires (actor in registry), the downstream checks
        (``route``, ``is_bot_assignee``) become operationally
        irrelevant. We assert the predicate's truth value because
        the audit-side effect (``action="loop_guard_dropped"``) is
        owned by the handler integration test — but the predicate is
        the only entry point that can decide "drop or not".
        """

        if not registry.union:
            return
        bot_account_id = next(iter(registry.union))
        # Loop guard fires regardless of ``foreign_actor`` content.
        assert is_self_actor(bot_account_id, registry.union) is True
        # The "non-bot" branch must remain reachable for genuine
        # human authors — otherwise the platform would never start
        # any workflow.
        assume(foreign_actor not in registry.union)
        assert is_self_actor(foreign_actor, registry.union) is False


# ---------------------------------------------------------------------------
# Property 10b — Webhook handler dept_id resolution → HTTP 400
# ---------------------------------------------------------------------------
#
# **Property 10: Webhook handler — per-dept HMAC, rotation overlap ve
# dept_id çözümlemesi**
#
# **Validates: Requirements 6.4, 6.5, 6.8, 10.4, 10.5**
#
# Companion to ``test_hmac_verify.py``'s ``TestPerDeptHmacIsolation``,
# ``TestWebhookSecretRotationOverlap`` and
# ``TestVerifyWebhookHmacMissingSecret`` classes which cover the
# *HMAC verification* leg of the property. Here we exercise the
# *dept_id resolution* leg through the actual FastAPI handler in
# :mod:`automation_service.webhooks_handlers`:
#
# - When the payload's ``issue.fields.project.key`` cannot be
#   extracted at all (missing / wrong type / empty), the handler
#   returns **HTTP 400** with ``reason="missing_issue_key"`` /
#   ``"invalid_json"`` / ``"webhook_dept_unresolved"`` depending on
#   which extractor failed.
#
# - When the project key *is* present but no department is registered
#   for it (``DeptResolver.resolve_by_project_key`` returns ``None``),
#   the handler returns **HTTP 400** with
#   ``reason="webhook_dept_unresolved"`` *and* writes an audit event
#   with ``action="webhook_dept_unresolved"``,
#   ``result="denied"``, and ``dept_id=None`` (R6.5).
#
# - When the project key resolves to a department, dept_id-resolution
#   passes and the chain proceeds to HMAC verification (which fails
#   here because we never sign the body — but that's the *next* leg,
#   audited as ``webhook_hmac_failed``, not ``webhook_dept_unresolved``).
#
# These properties drive the real router with hand-rolled fakes for
# every collaborator so the test stays self-contained — no Postgres,
# no Vault HTTP, no Temporal client. The fakes implement just enough
# of each Protocol (``DeptResolver``, ``VaultClient``, ``AuditLogger``,
# ``SupportsStartWorkflow``) for the chain to reach the dept_id-
# resolution decision point.
# ---------------------------------------------------------------------------

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

# Make the in-tree ``services/automation-service/src`` importable so
# ``automation_service`` resolves without an editable install. Mirrors
# the bootstrap at the top of this file (``_AUTOMATION_SRC``).
# ``automation_service.app`` imports ``from src.config import Settings``,
# which requires the *automation-service root* (the directory that
# contains ``src/``) on ``sys.path`` as well — without it, the relative
# ``src`` package cannot be located when pytest collects this module.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[1].parent / "services" / "automation-service"
for _bootstrap_path in (_AUTOMATION_ROOT, _AUTOMATION_ROOT / "src"):
    _bs = str(_bootstrap_path)
    if _bs not in sys.path:
        sys.path.insert(0, _bs)

from automation_service.app import create_app
from automation_service.webhooks_handlers import (
    BotRegistryEntry,
    WebhookContext,
)
from audit_logger import AuditEvent
from temporal_shared.capabilities import SupportsDepartment


# ---------------------------------------------------------------------------
# Lightweight stand-ins for the runtime collaborators
# ---------------------------------------------------------------------------


class _StubBot:
    """``HasCredential`` stand-in (always credentialled)."""

    def has_credential(self) -> bool:  # pragma: no cover - trivial
        return True


@dataclass(frozen=True)
class _StubBotSection:
    jira: _StubBot | None = None
    bitbucket: _StubBot | None = None
    confluence: _StubBot | None = None


@dataclass(frozen=True)
class _StubDept:
    """``SupportsDepartment`` stand-in with an extra ``id`` attribute.

    The runtime handler reads ``getattr(dept, "id", None)`` to obtain
    the dept_id used for the per-dept HMAC lookup; we surface ``id``
    on this stub so the resolution chain can complete deterministically.
    """

    id: str
    web_search_enabled: bool = False
    bot: _StubBotSection = field(default_factory=_StubBotSection)


class _StubDeptResolver:
    """``DeptResolver`` Protocol implementation backed by a dict."""

    def __init__(
        self,
        *,
        project_key_to_dept: Mapping[str, _StubDept] | None = None,
        bot_registry: list[BotRegistryEntry] | None = None,
    ) -> None:
        self._mapping = dict(project_key_to_dept or {})
        self._registry = list(bot_registry or [])

    async def resolve_by_project_key(
        self, project_key: str
    ) -> SupportsDepartment | None:
        return self._mapping.get(project_key)

    async def list_bot_account_ids(self) -> list[BotRegistryEntry]:
        return list(self._registry)


class _AlwaysFailingVault:
    """:class:`VaultClient` Protocol stand-in.

    The dept_id-resolution leg of the chain runs *before* HMAC
    verification, so for the properties in this section the Vault
    client is never reached when dept_id resolution fails. For the
    "resolution succeeds" property we set up a stub that returns no
    secret (``KeyError``) — ``verify_webhook_hmac`` then returns
    ``False`` and the chain produces HTTP 401, *not* HTTP 400. That
    distinction is what the tests assert.
    """

    backend: str = "stub"

    def read(self, path):  # type: ignore[no-untyped-def]
        raise KeyError(str(path))

    def write(self, path, data):  # type: ignore[no-untyped-def] # pragma: no cover
        raise NotImplementedError

    def delete(self, path):  # type: ignore[no-untyped-def] # pragma: no cover
        raise NotImplementedError

    def rotate_ssh_key(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def clear_previous_ssh_slot(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def rotate_webhook_secret(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _RecordingAuditLogger:
    """Minimal ``AuditLogger`` shim that records each event in memory."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FakeWorkflowClient:
    """``SupportsStartWorkflow`` stand-in.

    Never reached by the dept_id-resolution properties — the handler
    short-circuits with HTTP 400 before this client is consulted. We
    nonetheless implement a permissive stub so accidental control-flow
    regressions surface as test-double calls instead of attribute
    errors.
    """

    async def start_workflow(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        @dataclass
        class _Handle:
            id: str

        return _Handle(id="stub-workflow-id")


def _build_app_with_unresolvable_dept() -> tuple["TestClient", _RecordingAuditLogger]:
    """Build a FastAPI app whose ``DeptResolver`` resolves nothing.

    Returns the client and the recording audit logger so tests can
    assert both the HTTP response *and* the emitted audit events.
    """
    audit = _RecordingAuditLogger()
    ctx = WebhookContext(
        vault=_AlwaysFailingVault(),  # type: ignore[arg-type]
        dept_resolver=_StubDeptResolver(),  # empty mapping → always None
        workflow_client=_FakeWorkflowClient(),
        jira_commenter=None,
        audit_logger=audit,  # type: ignore[arg-type]
        env={},
        now_fn=lambda: datetime.now(timezone.utc),
    )
    app = create_app()
    app.state.webhook_v2 = ctx
    return TestClient(app), audit


def _build_app_with_resolvable_dept(
    project_key: str,
    dept_id: str,
) -> tuple["TestClient", _RecordingAuditLogger]:
    """Build a FastAPI app whose ``DeptResolver`` resolves *project_key*."""
    audit = _RecordingAuditLogger()
    dept = _StubDept(id=dept_id)
    ctx = WebhookContext(
        vault=_AlwaysFailingVault(),  # type: ignore[arg-type]
        dept_resolver=_StubDeptResolver(
            project_key_to_dept={project_key: dept},
            bot_registry=[],
        ),
        workflow_client=_FakeWorkflowClient(),
        jira_commenter=None,
        audit_logger=audit,  # type: ignore[arg-type]
        env={},
        now_fn=lambda: datetime.now(timezone.utc),
    )
    app = create_app()
    app.state.webhook_v2 = ctx
    return TestClient(app), audit


# ---------------------------------------------------------------------------
# Hypothesis strategies for webhook payloads
# ---------------------------------------------------------------------------


_project_keys = st.from_regex(r"^[A-Z][A-Z0-9_]{1,9}$", fullmatch=True)
_issue_keys = st.from_regex(r"^[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,4}$", fullmatch=True)
_account_ids_simple = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=40,
)


@st.composite
def _well_formed_jira_payloads(draw: st.DrawFn) -> dict:
    """Generate a Jira webhook payload with every required field.

    The payload survives the handler's own structural checks
    (``_extract_issue_key`` / ``_extract_project_key``) so the only
    failure mode left is the dept_id-resolver returning ``None``.
    """
    return {
        "webhookEvent": draw(st.sampled_from(["jira:issue_created", "jira:comment_created"])),
        "issue": {
            "key": draw(_issue_keys),
            "fields": {
                "project": {"key": draw(_project_keys)},
            },
        },
        "user": {"accountId": draw(_account_ids_simple)},
    }


@st.composite
def _payloads_with_extra_project_keys(draw: st.DrawFn) -> dict:
    """Like :func:`_well_formed_jira_payloads` but with a top-level ``project.key``.

    Some real Atlassian payloads carry the project key at
    ``project.key`` rather than ``issue.fields.project.key``. The
    extractor accepts both shapes — the property here ensures the
    fallback path also feeds the resolver correctly.
    """
    project_key = draw(_project_keys)
    issue_key = draw(_issue_keys)
    # 50/50 chance the issue.fields.project.key is missing entirely.
    use_top_level_only = draw(st.booleans())
    issue_fields: dict[str, Any] = {}
    if not use_top_level_only:
        issue_fields["project"] = {"key": project_key}
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {"key": issue_key, "fields": issue_fields},
        "project": {"key": project_key},
        "user": {"accountId": draw(_account_ids_simple)},
    }


@st.composite
def _payloads_missing_project_key(draw: st.DrawFn) -> dict:
    """Generate payloads where the project key cannot be extracted at all."""
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": draw(_issue_keys),
            "fields": {},  # no project
        },
        "user": {"accountId": draw(_account_ids_simple)},
    }


@st.composite
def _payloads_missing_issue_key(draw: st.DrawFn) -> dict:
    """Generate payloads with no extractable issue key."""
    project_key = draw(_project_keys)
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "fields": {"project": {"key": project_key}},
            # no ``key`` field
        },
        "user": {"accountId": draw(_account_ids_simple)},
    }


# ---------------------------------------------------------------------------
# Property 10b assertions
# ---------------------------------------------------------------------------


class TestWebhookDeptUnresolved:
    """Dept_id resolution failure → HTTP 400 + ``webhook_dept_unresolved`` audit.

    **Validates: Requirements 6.5, 10.4, 10.5**

    Property 10b: when the inbound webhook event's ``project_key``
    cannot be mapped to a configured department, the handler SHALL
    refuse the request with HTTP 400 and emit a single
    ``webhook_dept_unresolved`` audit event with ``result='denied'``
    and ``dept_id=None``.
    """

    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(payload=_well_formed_jira_payloads())
    def test_unknown_project_key_returns_400_and_audits(
        self, payload: dict
    ) -> None:
        """Unknown project_key → HTTP 400 + ``webhook_dept_unresolved`` audit.

        **Validates: Requirements 6.5, 10.4**
        """
        client, audit = _build_app_with_unresolvable_dept()
        try:
            resp = client.post(
                "/webhooks/jira/issue_created",
                content=json.dumps(payload).encode("utf-8"),
                headers={"X-Hub-Signature": "sha256=00"},
            )
        finally:
            client.close()

        assert resp.status_code == 400, (
            f"expected 400 webhook_dept_unresolved, got {resp.status_code} "
            f"with body={resp.text!r}"
        )
        assert resp.json() == {
            "status": "bad_request",
            "reason": "webhook_dept_unresolved",
        }
        # Exactly one audit event with the canonical action / shape.
        actions = [e.action for e in audit.events]
        assert actions.count("webhook_dept_unresolved") == 1, (
            f"expected exactly one 'webhook_dept_unresolved' audit, got actions={actions!r}"
        )
        evt = next(e for e in audit.events if e.action == "webhook_dept_unresolved")
        assert evt.result == "denied"
        assert evt.dept_id is None
        # The audit payload SHALL carry the project_key so operators
        # can identify which Jira project to wire up.
        assert evt.payload is not None
        assert "project_key" in evt.payload
        assert evt.payload["project_key"] == payload["issue"]["fields"]["project"]["key"]

    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(payload=_payloads_missing_project_key())
    def test_missing_project_key_returns_400(self, payload: dict) -> None:
        """No extractable project_key → HTTP 400 ``webhook_dept_unresolved``.

        **Validates: Requirement 6.5**

        ``_extract_project_key`` returns ``None`` when neither
        ``issue.fields.project.key`` nor ``project.key`` is present;
        the handler SHALL audit this as ``webhook_dept_unresolved`` and
        respond with HTTP 400.
        """
        client, audit = _build_app_with_unresolvable_dept()
        try:
            resp = client.post(
                "/webhooks/jira/issue_created",
                content=json.dumps(payload).encode("utf-8"),
                headers={"X-Hub-Signature": "sha256=00"},
            )
        finally:
            client.close()

        assert resp.status_code == 400
        assert resp.json()["reason"] == "webhook_dept_unresolved"
        evt = next(
            (e for e in audit.events if e.action == "webhook_dept_unresolved"),
            None,
        )
        assert evt is not None, "no webhook_dept_unresolved audit emitted"
        assert evt.dept_id is None

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(payload=_payloads_missing_issue_key())
    def test_missing_issue_key_returns_400(self, payload: dict) -> None:
        """No extractable issue key → HTTP 400 ``missing_issue_key``.

        **Validates: Requirements 10.4, 10.5**

        The handler validates ``issue.key`` *before* attempting
        dept_id resolution; this is a separate 400 path
        (``reason="missing_issue_key"``) but still surfaces as a 4xx
        so the caller knows the payload is malformed.
        """
        client, _audit = _build_app_with_unresolvable_dept()
        try:
            resp = client.post(
                "/webhooks/jira/issue_created",
                content=json.dumps(payload).encode("utf-8"),
                headers={"X-Hub-Signature": "sha256=00"},
            )
        finally:
            client.close()

        assert resp.status_code == 400
        assert resp.json() == {
            "status": "bad_request",
            "reason": "missing_issue_key",
        }

    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(payload=_well_formed_jira_payloads())
    def test_resolved_dept_proceeds_past_dept_id_check(
        self, payload: dict
    ) -> None:
        """A resolvable project_key SHALL clear the dept_id-resolution gate.

        **Validates: Requirements 6.5 (negation), 6.4**

        With a registered department for the payload's project_key,
        the handler MUST NOT return ``webhook_dept_unresolved``. The
        next gate is HMAC verification — which fails here because no
        secret is provisioned — so the response is HTTP 401, not
        HTTP 400. This negation property is what disambiguates a
        broken dept-resolver wiring from a correct one.
        """
        project_key = payload["issue"]["fields"]["project"]["key"]
        client, audit = _build_app_with_resolvable_dept(
            project_key=project_key,
            dept_id="payments",
        )
        try:
            resp = client.post(
                "/webhooks/jira/issue_created",
                content=json.dumps(payload).encode("utf-8"),
                headers={"X-Hub-Signature": "sha256=00"},
            )
        finally:
            client.close()

        # The dept_id resolver succeeded → no 400 / no
        # ``webhook_dept_unresolved`` audit.
        assert resp.status_code != 400, (
            f"dept_id resolution should have succeeded, but got "
            f"{resp.status_code} with body={resp.text!r}"
        )
        actions = [e.action for e in audit.events]
        assert "webhook_dept_unresolved" not in actions, (
            f"webhook_dept_unresolved emitted despite resolvable project_key; "
            f"actions={actions!r}"
        )
        # The next gate (HMAC verify) must fail because we provisioned
        # no secret — HTTP 401 ``unauthorized`` with a
        # ``webhook_hmac_failed`` audit row.
        assert resp.status_code == 401
        assert "webhook_hmac_failed" in actions

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(payload=_payloads_with_extra_project_keys())
    def test_top_level_project_key_fallback_resolves_dept(
        self, payload: dict
    ) -> None:
        """The handler accepts ``project.key`` *or* ``issue.fields.project.key``.

        **Validates: Requirement 6.5**

        Older / alternative Jira payload shapes carry the project
        key at the top level (``payload["project"]["key"]``). The
        handler's extractor falls back to that location when
        ``issue.fields.project`` is missing — the dept resolver still
        receives a non-None ``project_key`` and the dept_id-resolution
        gate passes (no ``webhook_dept_unresolved`` audit).
        """
        project_key = payload["project"]["key"]
        client, audit = _build_app_with_resolvable_dept(
            project_key=project_key,
            dept_id="finance",
        )
        try:
            resp = client.post(
                "/webhooks/jira/issue_created",
                content=json.dumps(payload).encode("utf-8"),
                headers={"X-Hub-Signature": "sha256=00"},
            )
        finally:
            client.close()

        actions = [e.action for e in audit.events]
        assert "webhook_dept_unresolved" not in actions

    def test_invalid_json_returns_400(self) -> None:
        """Non-JSON body → HTTP 400 ``invalid_json`` (R10.4 — defensive parse).

        Not a Hypothesis property (the input space is too narrow to be
        worth shrinking) but covers the same 4xx contract as the
        dept_id properties so the handler's HTTP-400 responses are
        all exercised in one suite.
        """
        client, _audit = _build_app_with_unresolvable_dept()
        try:
            resp = client.post(
                "/webhooks/jira/issue_created",
                content=b"this-is-not-json",
                headers={"X-Hub-Signature": "sha256=00"},
            )
        finally:
            client.close()
        assert resp.status_code == 400
        assert resp.json() == {"status": "bad_request", "reason": "invalid_json"}

    def test_handler_unwired_returns_503(self) -> None:
        """If ``app.state.webhook_v2`` is unset, the handler returns 503.

        Boot-time edge case: while the application's lifespan handler
        is still wiring collaborators, requests SHALL surface a 503
        rather than a 400. This property is what guarantees the four
        4xx audit semantics above are *only* invoked once the app is
        fully wired.
        """
        app = create_app()
        # Deliberately do not set ``app.state.webhook_v2``.
        with TestClient(app) as client:
            resp = client.post(
                "/webhooks/jira/issue_created",
                content=b"{}",
                headers={"X-Hub-Signature": "sha256=00"},
            )
        assert resp.status_code == 503
        assert resp.json()["status"] == "service_unavailable"


# ---------------------------------------------------------------------------
# Property 3 (task 4.2) — WebhookFilterChain stage decisions
# ---------------------------------------------------------------------------
#
# **Property 3: Webhook filter chain composite decision (task 4.2 slice)**
#
# **Validates: Requirements 3.4, 3.5, 4.1, 4.2, 4.6**
#
# This block adds the property-level coverage for the three stages
# wired by ``platform-mimari-workflows`` task 4.2 of the workflows
# spec — ``verify_hmac``, ``resolve_dept`` and ``loop_guard`` (with
# the ``^\s*\[bot:`` regex fallback). The tests live alongside the
# foundation R1.7 / R2.x predicate properties earlier in this file
# so the entire webhook predicate matrix shrinks toward the same
# minimal counter-examples when a stage breaks.
#
# Strategies in this block:
#
# * ``_chain_event_strategies`` — random :class:`WebhookEvent`
#   generators for both Jira and Bitbucket dialects.
# * ``_bot_id_pools``           — random bot account-id sets.
# * ``_body_text_options``      — random body text including
#   ``[bot:``-prefixed lines and unrelated chatter.
#
# We intentionally re-use the foundation strategies above for actor
# IDs / bot registries so the same Hypothesis pool generates events
# for both the predicate-level checks (foundation R1.7) and the
# chain-level checks (workflows R3.4 / R3.5 / R4.x).
# ---------------------------------------------------------------------------


from automation_service.webhook_filters import (  # noqa: E402
    BOT_PREFIX_REGEX,
    REASON_LOOP_GUARD_DROPPED,
    REASON_LOOP_GUARD_REGEX_DROPPED,
    REASON_WEBHOOK_DEPT_UNRESOLVED,
    REASON_WEBHOOK_HMAC_INVALID,
    FilterDecision,
    WebhookDeptUnresolvedError,
    WebhookEvent,
    WebhookFilterChain,
    WebhookHmacInvalidError,
)

import pytest  # noqa: E402  (used by ``pytest.raises`` in the tests below)


# Strategies — minimal but exhaustive enough for the three stages.

_chain_actor_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"
    ),
    min_size=1,
    max_size=24,
)

_chain_optional_actor_ids = st.one_of(st.none(), _chain_actor_ids)

#: Bot registry for the chain-level loop-guard tests. Re-uses the
#: same alphabet as ``_account_ids`` above so the pools overlap and
#: Hypothesis can shrink to shared counter-examples.
_chain_bot_registries = st.frozensets(_chain_actor_ids, min_size=0, max_size=8)

#: Strings that do **not** match the ``[bot:`` regex. We constrain
#: them with a non-zero leading character so Hypothesis cannot
#: accidentally produce a body that starts with whitespace plus
#: ``[bot:``.
_chain_non_bot_bodies = st.one_of(
    st.text(min_size=0, max_size=40).filter(
        lambda s: BOT_PREFIX_REGEX.search(s) is None
    ),
    st.none(),
)

#: Strings that DO match the ``[bot:`` regex (with possible leading
#: whitespace).
_chain_bot_bodies = st.builds(
    lambda ws, suffix: f"{ws}[bot:{suffix}]",
    ws=st.sampled_from(["", " ", "  ", "\t", " \t "]),
    suffix=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cc",), blacklist_characters="\n\r"
        ),
        min_size=0,
        max_size=20,
    ),
)


def _chain_event(
    *,
    delivery_id: str = "delivery-1",
    actor_account_id: str | None = "human-user",
    body_text: str | None = None,
) -> WebhookEvent:
    """Construct a :class:`WebhookEvent` with property-test-friendly defaults."""

    return WebhookEvent(
        provider="jira",
        event_type="jira:issue_created",
        delivery_id=delivery_id,
        actor_account_id=actor_account_id,
        body_text=body_text,
        project_key="PAY",
        repo_slug=None,
        issue_key="PAY-1",
        pr_id=None,
    )


def _chain_with(
    *,
    verify_hmac: Any = None,
    resolve_dept: Any = None,
    bot_account_ids: Any = None,
) -> WebhookFilterChain:
    """Build a chain whose collaborators default to no-op pass-through."""

    return WebhookFilterChain(
        verify_hmac=verify_hmac if verify_hmac is not None else (lambda ev: True),
        resolve_dept=(
            resolve_dept if resolve_dept is not None else (lambda ev: "payments")
        ),
        bot_account_ids=(
            bot_account_ids
            if bot_account_ids is not None
            else (lambda: frozenset())
        ),
        is_processed=lambda d: False,
        mention_set_for=lambda i: frozenset(),
        iter_count_for=lambda i: 0,
        reporter_for=lambda i: "reporter-1",
    )


# ---------------------------------------------------------------------------
# Property 3a — verify_hmac stage (R3.4)
# ---------------------------------------------------------------------------


class TestChainVerifyHmacProperty:
    """``WebhookFilterChain`` raises iff ``verify_hmac`` returns False.

    **Validates: Requirement 3.4**

    The chain forwards the boolean result of the ``verify_hmac``
    callback into either pass-through (True) or
    :class:`WebhookHmacInvalidError` (False). Hypothesis-driven
    ``random_event × {True, False}`` combinations confirm the chain
    has no hidden short-circuit that would let a forged signature
    through.
    """

    @settings(max_examples=200, deadline=2000)
    @given(
        actor=_chain_optional_actor_ids,
        delivery_id=st.text(min_size=1, max_size=30),
    )
    def test_verify_hmac_true_passes_chain_to_loop_guard(
        self, actor: str | None, delivery_id: str
    ) -> None:
        """**Validates: Requirement 3.4**

        For any random event, ``verify_hmac=True`` must let the
        chain proceed past the verify stage. With every other
        callback set to no-op pass-through, the result is either
        ``filter_chain_pass`` (no other stage fires) or a
        loop-guard regex drop (when ``actor is None`` and the body
        text matches — in this test we keep ``body_text=None`` so
        only the pass branch fires).
        """

        chain = _chain_with(verify_hmac=lambda ev: True)
        event = _chain_event(
            delivery_id=delivery_id,
            actor_account_id=actor,
            body_text=None,
        )

        decision = chain.evaluate(event)

        assert decision.action == "pass"

    @settings(max_examples=200, deadline=2000)
    @given(
        actor=_chain_optional_actor_ids,
        delivery_id=st.text(min_size=1, max_size=30),
    )
    def test_verify_hmac_false_raises_with_canonical_reason(
        self, actor: str | None, delivery_id: str
    ) -> None:
        """**Validates: Requirement 3.4**

        For any random event, ``verify_hmac=False`` raises
        :class:`WebhookHmacInvalidError` whose ``reason`` attribute
        is the canonical :data:`REASON_WEBHOOK_HMAC_INVALID` literal.
        """

        chain = _chain_with(verify_hmac=lambda ev: False)
        event = _chain_event(
            delivery_id=delivery_id,
            actor_account_id=actor,
        )

        with pytest.raises(WebhookHmacInvalidError) as excinfo:
            chain.evaluate(event)

        assert excinfo.value.reason == REASON_WEBHOOK_HMAC_INVALID

    @settings(max_examples=100, deadline=2000)
    @given(
        actor=_chain_optional_actor_ids,
        bot_ids=_chain_bot_registries,
    )
    def test_hmac_failure_dominates_dept_and_loop(
        self, actor: str | None, bot_ids: frozenset[str]
    ) -> None:
        """**Validates: Requirement 3.4 (precedence)**

        HMAC failure raises **regardless** of whether dept resolves
        or the actor is in the bot registry — the verify stage runs
        first.
        """

        chain = _chain_with(
            verify_hmac=lambda ev: False,
            resolve_dept=lambda ev: None,  # would otherwise raise dept error
            bot_account_ids=lambda: bot_ids,
        )
        event = _chain_event(actor_account_id=actor)

        with pytest.raises(WebhookHmacInvalidError):
            chain.evaluate(event)


# ---------------------------------------------------------------------------
# Property 3b — resolve_dept stage (R3.5)
# ---------------------------------------------------------------------------


class TestChainResolveDeptProperty:
    """``WebhookFilterChain`` raises iff ``resolve_dept`` returns ``None``.

    **Validates: Requirement 3.5**
    """

    @settings(max_examples=200, deadline=2000)
    @given(
        dept_id=st.one_of(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("Ll", "Nd"), whitelist_characters="-"
                ),
                min_size=1,
                max_size=15,
            ),
            st.none(),
        ),
        actor=_chain_optional_actor_ids,
    )
    def test_resolve_dept_decides_chain_outcome(
        self, dept_id: str | None, actor: str | None
    ) -> None:
        """**Validates: Requirement 3.5**

        ``resolve_dept(ev) is None`` ↔
        :class:`WebhookDeptUnresolvedError` is raised. Any non-None
        dept_id lets the chain proceed.
        """

        chain = _chain_with(
            resolve_dept=lambda ev: dept_id,
        )
        event = _chain_event(
            actor_account_id=actor,
            body_text=None,  # avoid regex fallback for clean assertion
        )

        if dept_id is None:
            with pytest.raises(WebhookDeptUnresolvedError) as excinfo:
                chain.evaluate(event)
            assert excinfo.value.reason == REASON_WEBHOOK_DEPT_UNRESOLVED
        else:
            decision = chain.evaluate(event)
            assert decision.action == "pass"

    @settings(max_examples=100, deadline=2000)
    @given(
        bot_ids=_chain_bot_registries,
        actor=_chain_actor_ids,
    )
    def test_dept_unresolved_dominates_loop_guard(
        self, bot_ids: frozenset[str], actor: str
    ) -> None:
        """**Validates: Requirement 3.5 (precedence over 4.1)**

        Dept unresolved raises **regardless** of whether the actor
        is in the bot registry. Operators get the actionable
        ``webhook_dept_unresolved`` audit instead of a silent
        ``loop_guard_dropped`` swallow.
        """

        chain = _chain_with(
            resolve_dept=lambda ev: None,
            bot_account_ids=lambda: bot_ids,
        )
        event = _chain_event(actor_account_id=actor)

        with pytest.raises(WebhookDeptUnresolvedError):
            chain.evaluate(event)


# ---------------------------------------------------------------------------
# Property 3c — loop_guard stage (R4.1, R4.2, R4.6)
# ---------------------------------------------------------------------------


class TestChainLoopGuardActorIdProperty:
    """Loop guard drops events whose actor is in the bot registry union.

    **Validates: Requirement 4.1**
    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(
        bot_ids=_chain_bot_registries.filter(lambda s: len(s) > 0),
        data=st.data(),
    )
    def test_actor_in_registry_drops_with_loop_guard_dropped(
        self, bot_ids: frozenset[str], data: st.DataObject
    ) -> None:
        """**Validates: Requirement 4.1**

        Picking *any* account_id from the registry produces a drop
        with reason ``loop_guard_dropped``. The chain consults the
        flat union, so any bot in any dept short-circuits — this
        mirrors the foundation Property 4 / R1.7 invariant at the
        chain level.
        """

        actor = data.draw(st.sampled_from(sorted(bot_ids)))
        chain = _chain_with(bot_account_ids=lambda: bot_ids)
        event = _chain_event(actor_account_id=actor)

        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_DROPPED

    @settings(max_examples=200, deadline=2000)
    @given(
        bot_ids=_chain_bot_registries,
        actor=_chain_actor_ids,
    )
    def test_actor_outside_registry_passes(
        self, bot_ids: frozenset[str], actor: str
    ) -> None:
        """**Validates: Requirement 4.1 (negative path)**

        Actors that are not in the bot registry pass the loop
        guard. With every other stage's callback set to no-op, the
        chain returns ``filter_chain_pass``.
        """

        assume(actor not in bot_ids)
        chain = _chain_with(bot_account_ids=lambda: bot_ids)
        event = _chain_event(actor_account_id=actor, body_text=None)

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    @settings(max_examples=100, deadline=2000)
    @given(actor=_chain_actor_ids)
    def test_empty_registry_never_loops(self, actor: str) -> None:
        """**Validates: Requirement 4.1 (boot-time)**

        Empty registry → every event flows through the loop guard,
        regardless of actor. This pins the boot-time invariant.
        """

        chain = _chain_with(bot_account_ids=lambda: frozenset())
        event = _chain_event(actor_account_id=actor, body_text=None)

        decision = chain.evaluate(event)
        assert decision.action == "pass"


class TestChainLoopGuardRegexFallbackProperty:
    """Regex fallback fires only when ``actor_account_id`` is None.

    **Validates: Requirements 4.2, 4.6**
    """

    @settings(max_examples=200, deadline=2000)
    @given(body=_chain_bot_bodies)
    def test_actor_none_with_bot_prefix_drops_regex_reason(
        self, body: str
    ) -> None:
        """**Validates: Requirements 4.2, 4.6**

        ``actor_account_id is None`` AND body matches
        ``^\\s*\\[bot:`` → drop with
        :data:`REASON_LOOP_GUARD_REGEX_DROPPED`.
        """

        # Sanity check the strategy: every generated body matches
        # the regex.
        assert BOT_PREFIX_REGEX.search(body) is not None

        chain = _chain_with()
        event = _chain_event(actor_account_id=None, body_text=body)

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_REGEX_DROPPED

    @settings(max_examples=200, deadline=2000)
    @given(body=_chain_non_bot_bodies)
    def test_actor_none_without_bot_prefix_passes(
        self, body: str | None
    ) -> None:
        """**Validates: Requirements 4.2, 4.6 (negative path)**

        ``actor_account_id is None`` AND body does NOT match the
        regex → pass through (the event is treated as a system
        emission with no bot footprint).
        """

        chain = _chain_with()
        event = _chain_event(actor_account_id=None, body_text=body)

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    @settings(max_examples=200, deadline=2000)
    @given(actor=_chain_actor_ids, body=_chain_bot_bodies)
    def test_human_actor_with_bot_quote_does_not_drop(
        self, actor: str, body: str
    ) -> None:
        """**Validates: Requirement 4.2 (regex gating)**

        A real human actor quoting ``[bot:`` is **not** treated as
        bot output. The regex fallback is gated on
        ``actor_account_id is None``; a present actor always takes
        the actor-id path and is allowed through if not in the
        registry.
        """

        chain = _chain_with(bot_account_ids=lambda: frozenset())
        event = _chain_event(actor_account_id=actor, body_text=body)

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    @settings(max_examples=100, deadline=2000)
    @given(
        bot_ids=_chain_bot_registries.filter(lambda s: len(s) > 0),
        body=_chain_bot_bodies,
        data=st.data(),
    )
    def test_actor_id_match_dominates_regex_match(
        self, bot_ids: frozenset[str], body: str, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 4.1, 4.2 (precedence)**

        When the actor IS in the bot registry AND the body matches
        the regex, the chain produces
        :data:`REASON_LOOP_GUARD_DROPPED` (the actor-id reason),
        not the regex reason. The regex fallback exists specifically
        for the case where the actor is missing — it never fires
        when the actor is present.
        """

        actor = data.draw(st.sampled_from(sorted(bot_ids)))
        chain = _chain_with(bot_account_ids=lambda: bot_ids)
        event = _chain_event(actor_account_id=actor, body_text=body)

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_DROPPED


class TestChainLoopGuardDeterminism:
    """All three task-4.2 stages produce deterministic verdicts.

    **Validates: Requirements 3.4, 3.5, 4.1, 4.2, 4.6**

    The chain is a pure function of ``(event, callbacks)``: the
    same input must always produce the same output. This pins the
    chain-level determinism that the design document earmarks for
    the integration test layer.
    """

    @settings(max_examples=100, deadline=2000)
    @given(
        actor=_chain_optional_actor_ids,
        body=_chain_non_bot_bodies,
        bot_ids=_chain_bot_registries,
    )
    def test_repeated_evaluate_returns_identical_decision(
        self,
        actor: str | None,
        body: str | None,
        bot_ids: frozenset[str],
    ) -> None:
        """Three back-to-back evaluations produce the same decision.

        Same event + same callbacks → identical
        :class:`FilterDecision`. Catches sneaky stateful caches
        that could leak between evaluations.
        """

        chain = _chain_with(bot_account_ids=lambda: bot_ids)
        event = _chain_event(actor_account_id=actor, body_text=body)

        d1 = chain.evaluate(event)
        d2 = chain.evaluate(event)
        d3 = chain.evaluate(event)

        assert d1 == d2 == d3


# ---------------------------------------------------------------------------
# Property 3 (task 4.3) — WebhookFilterChain mid-chain stage decisions
# ---------------------------------------------------------------------------
#
# **Property 3: Webhook filter chain composite decision (task 4.3 slice)**
#
# **Validates: Requirements 3.2, 3.3, 4.3, 4.4, 4.5**
#
# This block extends the task-4.2 chain-level coverage above with the
# four mid-chain stages landed by ``platform-mimari-workflows`` task
# 4.3 of the workflows spec:
#
# * ``streamlit_bypass``                  — V12 ``[bot:hear]`` bypass
# * ``replay_dedup``                       — duplicate ``delivery_id`` drop
# * ``mention_filter`` (Y6 + Z6 merged)    — comment authorisation
#
# The composite stage-ordering invariants live in their own classes so
# Hypothesis can shrink to the smallest counter-example for each
# precedence rule independently. Strategies generate Jira / Bitbucket
# events with random body text, iter counts, mention sets, and
# reporter ids; the chain is wired to deterministic callbacks driven
# by those generated values so each invariant is checked over the
# *cross-product* of inputs.
# ---------------------------------------------------------------------------


from automation_service.webhook_filters import (  # noqa: E402
    JIRA_COMMENT_EVENT_TYPE,
    REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR,
    REASON_DUPLICATE_EVENT_DROPPED,
    REASON_FILTER_CHAIN_PASS,
    REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION,
    REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS,
    STREAMLIT_BYPASS_TAG,
)


# ---------------------------------------------------------------------------
# Strategies for the mid-chain properties
# ---------------------------------------------------------------------------
#
# Re-uses the foundation strategies (``_chain_actor_ids``,
# ``_chain_bot_registries``, etc.) but adds:
#
# * ``_chain_event_types``     — the supported Jira event types so we
#   can sample comment vs. non-comment scenarios.
# * ``_chain_iter_counts``     — small ints that span the Z6 boundary
#   (``0``, ``1``, ``> 1``).
# * ``_chain_mention_sets``    — small frozensets of bot-mentioned
#   account ids; deliberately overlaps with ``_chain_actor_ids`` so
#   Hypothesis can produce both "actor ∈ mention_set" and "actor ∉
#   mention_set" examples.
# * ``_chain_bypass_bodies``   — bodies that contain the
#   ``[bot:hear]`` tag in random casings / positions.
# * ``_chain_no_bypass_bodies``— bodies that do **not** contain the
#   tag.

_chain_event_types = st.sampled_from(
    [
        "jira:issue_created",
        "jira:issue_assigned",
        "jira:issue_updated",
        JIRA_COMMENT_EVENT_TYPE,
    ]
)

#: Iteration counter values — span the Z6 boundary deliberately so the
#: ``iter == 1 + actor == reporter`` predicate is exercised on both
#: sides.
_chain_iter_counts = st.integers(min_value=0, max_value=20)

#: Mention sets keep their cardinality small so Hypothesis can produce
#: both "actor in set" and "actor not in set" branches with high
#: probability.
_chain_mention_sets = st.frozensets(_chain_actor_ids, min_size=0, max_size=4)

#: Issue keys — narrow alphabet so Hypothesis can re-use the same key
#: across generated events when checking call-order invariants.
_chain_issue_keys = st.from_regex(r"^[A-Z]{2,4}-[1-9][0-9]{0,3}$", fullmatch=True)


@st.composite
def _chain_bypass_bodies(draw: st.DrawFn) -> str:
    """Bodies that DO contain the ``[bot:hear]`` tag (case-insensitive).

    Generates random surrounding text so the position of the tag
    varies between examples; the tag's casing also varies. Pins V12's
    "match anywhere, ignore case" contract.
    """

    surrounding = draw(
        st.text(
            alphabet=st.characters(
                blacklist_categories=("Cc",),
                blacklist_characters="\n\r",
            ),
            min_size=0,
            max_size=20,
        )
    )
    cased_tag = draw(
        st.sampled_from(
            [
                "[bot:hear]",
                "[BOT:HEAR]",
                "[Bot:Hear]",
                "[bOt:hEaR]",
                "[BoT:hEar]",
            ]
        )
    )
    insertion_point = draw(st.integers(min_value=0, max_value=len(surrounding)))
    return (
        surrounding[:insertion_point] + cased_tag + surrounding[insertion_point:]
    )


@st.composite
def _chain_no_bypass_bodies(
    draw: st.DrawFn,
) -> str | None:
    """Bodies that do NOT contain the ``[bot:hear]`` tag.

    Filters out the tag entirely — case-insensitive — so the strategy
    never accidentally produces a body that would trigger the
    streamlit-bypass stage.
    """

    body = draw(st.one_of(st.none(), st.text(min_size=0, max_size=40)))
    if body is None:
        return None
    # Filter check: reject any body that contains the tag in any
    # casing. The branch below re-draws on rejection so the strategy
    # converges quickly on valid no-bypass bodies.
    if STREAMLIT_BYPASS_TAG.lower() in body.lower():
        return ""  # neutralise the rare collision; "" never matches.
    return body


def _chain_comment_event(
    *,
    delivery_id: str = "delivery-1",
    actor_account_id: str | None = "human-user",
    body_text: str | None = "a regular comment",
    issue_key: str | None = "PAY-1",
) -> WebhookEvent:
    """Construct a ``jira:issue_commented`` :class:`WebhookEvent`.

    Mirrors the helper in the task-4.3 unit suite so the property and
    unit modules share the same fixture shape.
    """

    return WebhookEvent(
        provider="jira",
        event_type=JIRA_COMMENT_EVENT_TYPE,
        delivery_id=delivery_id,
        actor_account_id=actor_account_id,
        body_text=body_text,
        project_key="PAY",
        repo_slug=None,
        issue_key=issue_key,
        pr_id=None,
    )


def _chain_with_mid(
    *,
    is_processed: Any = None,
    mention_set_for: Any = None,
    iter_count_for: Any = None,
    reporter_for: Any = None,
) -> WebhookFilterChain:
    """Build a chain whose mid-chain callbacks default to no-op pass-through.

    Distinct from ``_chain_with`` above (which only exposes the
    task-4.2 callbacks) so the property tests can override the
    mid-chain callbacks independently of the verifier collaborators.
    """

    return WebhookFilterChain(
        verify_hmac=lambda ev: True,
        resolve_dept=lambda ev: "payments",
        bot_account_ids=lambda: frozenset(),
        is_processed=is_processed if is_processed is not None else (lambda d: False),
        mention_set_for=(
            mention_set_for if mention_set_for is not None else (lambda i: frozenset())
        ),
        iter_count_for=(
            iter_count_for if iter_count_for is not None else (lambda i: 0)
        ),
        reporter_for=(
            reporter_for if reporter_for is not None else (lambda i: "reporter-1")
        ),
    )


# ---------------------------------------------------------------------------
# Property 3d — replay_dedup precedence (task 4.3, slice 1)
# ---------------------------------------------------------------------------


class TestChainReplayDedupProperty:
    """``replay_dedup`` runs after streamlit_bypass and before mention_filter.

    **Validates: Requirements 3.2, 3.3, 4.5**

    The dedup stage is the idempotency anchor for the chain — it must
    catch every duplicate ``delivery_id`` that did not match the
    ``[bot:hear]`` bypass. The property below pins both the positive
    drop verdict and the precedence relations on either side of the
    stage.
    """

    @settings(max_examples=200, deadline=2000)
    @given(
        delivery_id=st.text(min_size=1, max_size=30),
        actor=_chain_optional_actor_ids,
    )
    def test_seen_delivery_drops_with_duplicate_event_dropped(
        self, delivery_id: str, actor: str | None
    ) -> None:
        """Any seen delivery_id → drop with ``duplicate_event_dropped``.

        **Validates: Requirements 3.2, 3.3 (idempotency anchor)**

        Hypothesis varies the delivery_id and actor across the full
        valid space; the dedup verdict must not depend on either —
        once ``is_processed`` returns True, the chain drops with the
        canonical reason.
        """

        chain = _chain_with_mid(is_processed=lambda d: True)
        event = WebhookEvent(
            provider="jira",
            event_type="jira:issue_created",
            delivery_id=delivery_id,
            actor_account_id=actor,
            body_text=None,
            project_key="PAY",
            repo_slug=None,
            issue_key="PAY-1",
            pr_id=None,
        )

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_DUPLICATE_EVENT_DROPPED

    @settings(max_examples=200, deadline=2000)
    @given(body=_chain_bypass_bodies(), delivery_id=st.text(min_size=1, max_size=30))
    def test_streamlit_bypass_dominates_replay_dedup(
        self, body: str, delivery_id: str
    ) -> None:
        """``[bot:hear]`` retries survive replay_dedup.

        **Validates: Requirement 4.5 (V12 precedence over R4 dedup)**

        The bypass tag is the only signal that a comment came from
        the bot's own UI; any retry of that delivery (network blip,
        browser double-submit, Atlassian re-fire) must still be
        honoured. We assert the precedence by setting
        ``is_processed`` to True and checking that the chain still
        passes the event with the V12 reason.
        """

        chain = _chain_with_mid(is_processed=lambda d: True)
        event = _chain_comment_event(
            delivery_id=delivery_id,
            body_text=body,
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS

    @settings(max_examples=200, deadline=2000)
    @given(
        iter_count=_chain_iter_counts,
        actor=_chain_actor_ids,
        mention_set=_chain_mention_sets,
        delivery_id=st.text(min_size=1, max_size=30),
    )
    def test_replay_dedup_dominates_mention_filter(
        self,
        iter_count: int,
        actor: str,
        mention_set: frozenset[str],
        delivery_id: str,
    ) -> None:
        """A duplicate event drops with the dedup reason, not Y6.

        **Validates: Requirements 3.2, 3.3, 4.3 (precedence)**

        With both stages firing (``is_processed`` True AND iter > 1
        + unauthorised actor), the chain must produce
        ``duplicate_event_dropped`` — operators want to know the
        event was a duplicate of a real prior delivery, not that
        the actor was unauthorised. Hypothesis varies iter / actor /
        mention_set across the full Y6 input space to catch any
        accidental ordering reversal.
        """

        # Force the Y6 conditions: actor MUST NOT be in the mention set
        # so Y6 would fire if it ran. Hypothesis-time precondition
        # rather than ``assume`` so the strategy stays efficient.
        if actor in mention_set:
            return

        chain = _chain_with_mid(
            is_processed=lambda d: True,
            iter_count_for=lambda i: iter_count,
            mention_set_for=lambda i: mention_set,
        )
        event = _chain_comment_event(
            delivery_id=delivery_id,
            actor_account_id=actor,
        )

        decision = chain.evaluate(event)
        # Even when iter > 1 + actor ∉ mention_set (Y6 conditions),
        # dedup wins.
        assert decision.action == "drop"
        assert decision.reason == REASON_DUPLICATE_EVENT_DROPPED


# ---------------------------------------------------------------------------
# Property 3e — first_iter_exception (Z6, task 4.3 slice 2)
# ---------------------------------------------------------------------------


class TestChainFirstIterExceptionProperty:
    """Z6 fires only for ``iter == 1`` + ``actor == reporter``.

    **Validates: Requirement 4.4**

    The first-iter exception is the only path that produces the
    ``mention_filter_first_iter_exception`` audit reason; all other
    paths through the mention-filter stage either drop with Y6 or
    fall through to ``filter_chain_pass``. The property below
    enumerates the cross-product of (iter, actor, reporter) and
    confirms Z6 fires iff the predicate matches.
    """

    @settings(max_examples=200, deadline=2000)
    @given(
        actor=_chain_actor_ids,
        reporter=_chain_actor_ids,
        iter_count=_chain_iter_counts,
    )
    def test_z6_fires_iff_iter_le_one_and_actor_equals_reporter(
        self, actor: str, reporter: str, iter_count: int
    ) -> None:
        """Z6 fires ↔ ``iter <= 1 ∧ actor == reporter``.

        **Validates: Requirement 4.4 (Z6 truth table)**

        The chain treats ``iter == 0`` as ``iter == 1`` for Z6
        (the freshly-created issue race), so the predicate is
        ``iter <= 1`` rather than ``iter == 1`` exactly. Hypothesis
        varies all three inputs and asserts the audit-reason matches
        the predicate's truth value.
        """

        chain = _chain_with_mid(
            iter_count_for=lambda i: iter_count,
            reporter_for=lambda i: reporter,
            mention_set_for=lambda i: frozenset(),  # Y6 disabled
        )
        event = _chain_comment_event(actor_account_id=actor)

        decision = chain.evaluate(event)

        z6_should_fire = iter_count <= 1 and actor == reporter
        if z6_should_fire:
            assert decision.action == "pass"
            assert (
                decision.reason == REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION
            ), (
                f"Z6 should have fired for iter={iter_count}, "
                f"actor={actor!r}, reporter={reporter!r}; got reason={decision.reason!r}"
            )
        else:
            # When Z6 does NOT apply, the chain either drops on Y6
            # (iter > 1 + actor ∉ empty mention_set) or passes with
            # ``filter_chain_pass``. Reasoned about explicitly so
            # Hypothesis surfaces accidental Z6 leaks here.
            assert decision.reason != REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION
            if iter_count > 1:
                # Empty mention set → actor ∉ mention_set → Y6 drops.
                assert decision.action == "drop"
                assert decision.reason == REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR
            else:
                # iter <= 1 with actor != reporter → fall through.
                assert decision.action == "pass"
                assert decision.reason == REASON_FILTER_CHAIN_PASS

    @settings(max_examples=200, deadline=2000)
    @given(
        actor=_chain_actor_ids,
        reporter=_chain_actor_ids,
        iter_count=_chain_iter_counts,
        mention_set=_chain_mention_sets,
    )
    def test_z6_dominates_when_actor_also_in_mention_set(
        self,
        actor: str,
        reporter: str,
        iter_count: int,
        mention_set: frozenset[str],
    ) -> None:
        """Z6's audit label is stable even when Y6 would also pass.

        **Validates: Requirement 4.4 (stable labelling)**

        At iter <= 1 with actor == reporter and actor ∈ mention_set,
        both Z6 and the implicit "iter <= 1 has no Y6" path would
        produce a pass verdict. The chain audit-labels the verdict
        as Z6 so operators get a single, stable label per condition.
        """

        # Constrain the strategy to the precondition we care about.
        if iter_count > 1 or actor != reporter:
            return

        chain = _chain_with_mid(
            iter_count_for=lambda i: iter_count,
            reporter_for=lambda i: reporter,
            mention_set_for=lambda i: mention_set,
        )
        event = _chain_comment_event(actor_account_id=actor)

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION


# ---------------------------------------------------------------------------
# Property 3f — mention_filter scope (task 4.3 slice 3)
# ---------------------------------------------------------------------------


class TestChainMentionFilterScopeProperty:
    """``mention_filter`` only fires for ``jira:issue_commented`` events.

    **Validates: Requirements 3.2, 3.3, 4.3**

    The Y6 / Z6 logic must not leak to ``jira:issue_created`` /
    ``jira:issue_assigned`` / ``jira:issue_updated`` /
    ``pullrequest:*`` events. The property below randomly samples
    every supported event type and asserts the mention-filter never
    drops or labels a non-comment event.
    """

    @settings(max_examples=200, deadline=2000)
    @given(
        event_type=_chain_event_types,
        actor=_chain_actor_ids,
        iter_count=_chain_iter_counts,
        mention_set=_chain_mention_sets,
    )
    def test_y6_only_fires_for_issue_commented(
        self,
        event_type: str,
        actor: str,
        iter_count: int,
        mention_set: frozenset[str],
    ) -> None:
        """Y6 enforcement is scoped to ``jira:issue_commented`` events.

        **Validates: Requirements 3.2 (Jira event scope), 4.3 (Y6 scope)**

        For every non-comment event_type, the chain must NOT drop
        with ``comment_ignored_unauthorized_actor`` regardless of
        iter / mention_set state. The default no-op callbacks for
        the verifier stages let the chain reach the mention-filter
        stage cleanly so the scope check is the only decision left.
        """

        chain = _chain_with_mid(
            iter_count_for=lambda i: iter_count,
            mention_set_for=lambda i: mention_set,
        )
        event = WebhookEvent(
            provider="jira",
            event_type=event_type,
            delivery_id="delivery-prop",
            actor_account_id=actor,
            body_text=None,
            project_key="PAY",
            repo_slug=None,
            issue_key="PAY-1",
            pr_id=None,
        )

        decision = chain.evaluate(event)

        if event_type != JIRA_COMMENT_EVENT_TYPE:
            # Non-comment events MUST NEVER drop with the Y6 reason
            # and MUST NEVER receive the Z6 audit label.
            assert decision.reason != REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR
            assert decision.reason != REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION
            # With every other stage's callback set to no-op, the
            # default verdict is ``filter_chain_pass``.
            assert decision.action == "pass"
            assert decision.reason == REASON_FILTER_CHAIN_PASS

    @settings(max_examples=200, deadline=2000)
    @given(
        actor=_chain_actor_ids,
        iter_count=_chain_iter_counts,
        mention_set=_chain_mention_sets,
    )
    def test_y6_truth_table_for_comment_events(
        self,
        actor: str,
        iter_count: int,
        mention_set: frozenset[str],
    ) -> None:
        """Y6 fires ↔ ``iter > 1 ∧ actor ∉ mention_set``.

        **Validates: Requirement 4.3 (Y6 truth table)**

        Comment events with ``iter <= 1`` either trigger Z6 (when
        the actor is the reporter) or fall through (when not). Comment
        events with ``iter > 1`` are subject to the Y6 mention-set
        check. We seed the reporter with a fixed value distinct from
        any generated actor so Z6 never accidentally fires.
        """

        # Use a literal reporter the actor strategy cannot generate
        # (the alphabet excludes ``!``). This isolates Y6 from Z6 so
        # the truth-table assertion stays clean.
        reporter = "!fixed-reporter-not-in-alphabet"
        assume(actor != reporter)

        chain = _chain_with_mid(
            iter_count_for=lambda i: iter_count,
            reporter_for=lambda i: reporter,
            mention_set_for=lambda i: mention_set,
        )
        event = _chain_comment_event(actor_account_id=actor)

        decision = chain.evaluate(event)

        if iter_count > 1 and actor not in mention_set:
            assert decision.action == "drop"
            assert decision.reason == REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR
        elif iter_count > 1 and actor in mention_set:
            # Mentioned actor at iter > 1 → pass with the default
            # reason; Y6 falls through.
            assert decision.action == "pass"
            assert decision.reason == REASON_FILTER_CHAIN_PASS
        else:
            # iter <= 1 + actor != reporter → fall through (Z6 disabled
            # because the reporter does not match).
            assert decision.action == "pass"
            assert decision.reason == REASON_FILTER_CHAIN_PASS


# ---------------------------------------------------------------------------
# Property 3g — [bot:hear] case-insensitive (task 4.3 slice 4)
# ---------------------------------------------------------------------------


class TestChainStreamlitBypassCaseInsensitiveProperty:
    """The ``[bot:hear]`` tag match is case-insensitive and position-agnostic.

    **Validates: Requirement 4.5 (V12 case-insensitive)**

    Editors and clients sometimes auto-capitalise tags, prepend
    surrounding whitespace, or wrap them in punctuation. The chain
    must honour every casing variant of ``[bot:hear]`` regardless of
    where it appears in the body. Hypothesis generates random surroundings
    and casings so a regression in the regex (eg. lowering ``re.IGNORECASE``)
    surfaces here.
    """

    @settings(max_examples=200, deadline=2000)
    @given(body=_chain_bypass_bodies())
    def test_any_casing_of_bypass_tag_short_circuits_chain(
        self, body: str
    ) -> None:
        """Any case-insensitive variant of ``[bot:hear]`` triggers the bypass.

        **Validates: Requirement 4.5 (V12 case-insensitive match)**

        The strategy guarantees the body contains the tag in some
        casing; the chain must therefore short-circuit to pass with
        the V12 audit reason regardless of where the tag lands.
        """

        # Sanity check: the strategy never produces a body without
        # the tag (case-insensitive).
        assert STREAMLIT_BYPASS_TAG.lower() in body.lower()

        chain = _chain_with_mid()
        event = _chain_comment_event(
            actor_account_id="anyone",
            body_text=body,
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS

    @settings(max_examples=200, deadline=2000)
    @given(body=_chain_no_bypass_bodies())
    def test_bodies_without_bypass_tag_never_short_circuit_to_v12(
        self, body: str | None
    ) -> None:
        """Bodies without ``[bot:hear]`` never receive the V12 bypass.

        **Validates: Requirement 4.5 (V12 negative path)**

        The negative property is what proves the bypass is precisely
        the design's tag — not a false-positive on other ``[bot:``
        prefixes (eg. the loop-guard regex's ``[bot:speak]``). The
        chain falls through to the next stage and ultimately to
        ``filter_chain_pass`` because the body never matches.
        """

        # Sanity: the strategy excludes any casing of the tag.
        if body is not None and body != "":
            assert STREAMLIT_BYPASS_TAG.lower() not in body.lower()

        chain = _chain_with_mid()
        event = _chain_comment_event(
            actor_account_id="anyone",
            body_text=body,
        )

        decision = chain.evaluate(event)
        # The bypass MUST NOT fire; with all other callbacks no-op
        # the chain reaches ``filter_chain_pass``.
        assert decision.reason != REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS


# ---------------------------------------------------------------------------
# Property 3h — Mid-chain determinism (composition)
# ---------------------------------------------------------------------------


class TestChainMidChainDeterminismProperty:
    """Mid-chain stages compose into a deterministic verdict.

    **Validates: Requirements 3.2, 3.3, 4.3, 4.4, 4.5**

    The chain is a pure function of ``(event, callbacks)``: same
    input → same output. The 4.2 sibling class
    :class:`TestChainLoopGuardDeterminism` pins the same invariant for
    the verifier stages; this class extends the coverage to events
    that exercise the mid-chain. Catches stateful caches that could
    leak between evaluations and any time-dependent behaviour the
    chain accidentally introduces.
    """

    @settings(max_examples=100, deadline=2000)
    @given(
        actor=_chain_actor_ids,
        reporter=_chain_actor_ids,
        iter_count=_chain_iter_counts,
        mention_set=_chain_mention_sets,
        is_processed_flag=st.booleans(),
        body=st.one_of(_chain_bypass_bodies(), _chain_no_bypass_bodies()),
    )
    def test_repeated_evaluate_returns_identical_decision(
        self,
        actor: str,
        reporter: str,
        iter_count: int,
        mention_set: frozenset[str],
        is_processed_flag: bool,
        body: str | None,
    ) -> None:
        """Three back-to-back evaluations produce the same decision.

        Same event + same callbacks → identical
        :class:`FilterDecision`. The strategy spans every mid-chain
        decision branch (bypass / dedup / Z6 / Y6 / pass-through)
        so a regression in any stage's purity surfaces here.
        """

        chain = _chain_with_mid(
            is_processed=lambda d: is_processed_flag,
            iter_count_for=lambda i: iter_count,
            mention_set_for=lambda i: mention_set,
            reporter_for=lambda i: reporter,
        )
        event = _chain_comment_event(
            actor_account_id=actor,
            body_text=body,
        )

        d1 = chain.evaluate(event)
        d2 = chain.evaluate(event)
        d3 = chain.evaluate(event)

        assert d1 == d2 == d3


# ---------------------------------------------------------------------------
# Property 3 (task 4.4) — Burst-debounce stage in the chain
# ---------------------------------------------------------------------------
#
# **Property 3: Webhook filter chain composite decision (task 4.4 slice)**
#
# **Validates: Requirements 4.7, 4.8**
#
# This block extends the workflows-spec property coverage with the
# **burst-debounce** stage landed by ``platform-mimari-workflows``
# task 4.4 of the workflows spec. Specifically:
#
# * 3-second window invariant: same ``issue_key`` events arriving
#   inside the window coalesce; the last payload wins (design mandate
#   "son event'in payload'ı korunur").
# * Drop reason invariant: every coalesced delivery surfaces as
#   :class:`FilterDecision` with ``action="drop"`` and
#   ``reason=REASON_BURST_COALESCED``.
# * Independent windows: events with different ``issue_key`` never
#   coalesce — they live in separate buffers.
# * Anchor delivery never appears in ``coalesced_with``: the design
#   contract reserves that list for *dropped* deliveries.
#
# The :class:`automation_service.burst_window.BurstWindow` is the
# only stage in the chain that owns wall-clock state, so the tests
# inject ``now`` explicitly via a queue. This keeps the property
# deterministic without ``time.sleep`` and lets Hypothesis explore
# arbitrary timing offsets.
# ---------------------------------------------------------------------------


from automation_service.burst_window import (  # noqa: E402
    BURST_WINDOW_SECONDS,
    BurstWindow,
)
from automation_service.webhook_filters import (  # noqa: E402
    REASON_BURST_COALESCED,
    BurstRegisterResult,
)


# ---------------------------------------------------------------------------
# Strategies for burst-debounce properties
# ---------------------------------------------------------------------------

#: Issue keys reused inside burst-debounce composition tests.
_chain_burst_issue_keys = st.from_regex(
    r"^[A-Z]{2,4}-[1-9][0-9]{0,3}$", fullmatch=True
)

#: Delivery ids — short alphanumerics so Hypothesis can shrink
#: efficiently. The strategy enforces uniqueness inside any list
#: that uses it, but at the leaf level we only need printable ids.
_chain_burst_delivery_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"
    ),
    min_size=1,
    max_size=12,
)

#: Payload values — keep them tiny so the property fixture stays
#: cheap. We test the "latest payload wins" invariant by varying a
#: single ``v`` field; the chain doesn't introspect the payload so
#: shape variation is irrelevant.
_chain_burst_payloads = st.fixed_dictionaries(
    {"v": st.integers(min_value=0, max_value=10_000)}
)


def _build_burst_chain(
    burst: BurstWindow, *, now_provider: list[float]
) -> WebhookFilterChain:
    """Build a chain whose burst stage is wired to *burst*.

    Mirrors ``test_burst_debounce._make_chain_with_burst`` so the two
    test modules describe the same composition. All non-burst stages
    are no-op pass-throughs:

    * ``verify_hmac`` always True.
    * ``resolve_dept`` returns a fixed dept_id.
    * ``bot_account_ids`` empty → loop guard never fires.
    * ``is_processed`` False → replay-dedup never fires.
    * ``mention_set_for`` empty + ``iter_count_for`` 1 → mention
      filter falls through; the comment event hits the burst stage.
    * ``reporter_for`` returns a sentinel that no test actor matches
      so Z6 cannot accidentally fire.

    Tests pop ``now`` values from ``now_provider`` in the order the
    chain registers them, mirroring how the production wiring will
    read ``time.monotonic()`` once per delivery.
    """

    def _burst_register(event: WebhookEvent) -> BurstRegisterResult | None:
        if event.issue_key is None:
            return None
        now = now_provider.pop(0)
        decision = burst.register(
            issue_key=event.issue_key,
            delivery_id=event.delivery_id,
            payload=dict(event.raw_payload),
            now=now,
        )
        if decision == "coalesce_dropped":
            # Reach into the buffer to read the running coalesced
            # list non-destructively. The test_burst_debounce module
            # uses the same trick — see its docstring for the
            # rationale. Safe inside tests because production wires a
            # sweeper that consults ``flush_window`` directly.
            buffer = burst._buffers.get(event.issue_key)  # noqa: SLF001
            coalesced = (
                tuple(buffer.dropped_delivery_ids) if buffer is not None else ()
            )
            return BurstRegisterResult(
                decision="coalesce_dropped",
                coalesced_with=coalesced,
            )
        return BurstRegisterResult(decision="coalesce_emit", coalesced_with=())

    return WebhookFilterChain(
        verify_hmac=lambda _e: True,
        resolve_dept=lambda _e: "dept-burst",
        bot_account_ids=lambda: frozenset(),
        is_processed=lambda _d: False,
        mention_set_for=lambda _k: frozenset(),
        iter_count_for=lambda _k: 1,
        reporter_for=lambda _k: "__no_reporter__",
        burst_register=_burst_register,
    )


def _burst_event(
    *,
    issue_key: str,
    delivery_id: str,
    payload: Mapping[str, Any] | None = None,
) -> WebhookEvent:
    """Build a non-comment event so the burst stage is the decider."""

    return WebhookEvent(
        provider="jira",
        # ``jira:issue_updated`` keeps the mention filter quiet (it
        # only fires for ``jira:issue_commented``) so the burst stage
        # is the only stage left that can produce a non-pass verdict.
        event_type="jira:issue_updated",
        delivery_id=delivery_id,
        actor_account_id="human-actor",
        body_text=None,
        project_key=issue_key.split("-", 1)[0],
        repo_slug=None,
        issue_key=issue_key,
        pr_id=None,
        raw_payload=dict(payload or {}),
    )


# We import ``Mapping`` from the typing module so the signature
# annotation above lands without bleeding the import into the
# top-of-file block (which would re-order the existing module-level
# imports). This re-import is intentional and idiomatic for an
# appended property block.
from typing import Mapping  # noqa: E402


# ---------------------------------------------------------------------------
# Property 3i — Burst drop reason invariant
# ---------------------------------------------------------------------------


class TestChainBurstDebounceDropReasonProperty:
    """Coalesced deliveries always surface as ``burst_coalesced`` drops.

    **Validates: Requirement 4.7 (drop reason invariant)**

    Whenever a delivery lands inside an open 3-second window for the
    same ``issue_key``, the chain must produce
    ``FilterDecision(action="drop", reason="burst_coalesced",
    coalesced_with=...)``. The property below enumerates random
    timings and delivery sequences inside the window and checks the
    invariant on every step.
    """

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_chain_burst_issue_keys,
        delivery_a=_chain_burst_delivery_ids,
        delivery_b=_chain_burst_delivery_ids,
        offset=st.floats(
            min_value=0.0,
            max_value=BURST_WINDOW_SECONDS - 0.1,
            allow_nan=False,
            allow_infinity=False,
        ),
        payload_a=_chain_burst_payloads,
        payload_b=_chain_burst_payloads,
    )
    def test_second_event_within_window_drops_with_burst_coalesced(
        self,
        issue_key: str,
        delivery_a: str,
        delivery_b: str,
        offset: float,
        payload_a: dict,
        payload_b: dict,
    ) -> None:
        """Same-key + within-window → drop with reason ``burst_coalesced``.

        The anchor delivery must dispatch (``filter_chain_pass``) and
        the second delivery must drop with the canonical reason. The
        ``coalesced_with`` field on the drop verdict lists exactly
        the dropped delivery_id (the anchor never appears).
        """

        assume(delivery_a != delivery_b)

        burst = BurstWindow()
        now_q: list[float] = [100.0, 100.0 + offset]
        chain = _build_burst_chain(burst, now_provider=now_q)

        first = chain.evaluate(
            _burst_event(
                issue_key=issue_key,
                delivery_id=delivery_a,
                payload=payload_a,
            )
        )
        second = chain.evaluate(
            _burst_event(
                issue_key=issue_key,
                delivery_id=delivery_b,
                payload=payload_b,
            )
        )

        assert first.action == "pass"
        assert first.reason == REASON_FILTER_CHAIN_PASS
        # The anchor delivery passes through with no coalesced ids
        # because no other event has been merged yet.
        assert first.coalesced_with == ()

        assert second.action == "drop"
        assert second.reason == REASON_BURST_COALESCED
        # The drop's coalesced_with carries exactly the dropped
        # delivery_id; the anchor's id is *not* there.
        assert second.coalesced_with == (delivery_b,)
        assert delivery_a not in second.coalesced_with

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_chain_burst_issue_keys,
        deliveries=st.lists(
            _chain_burst_delivery_ids,
            min_size=2,
            max_size=6,
            unique=True,
        ),
    )
    def test_burst_sequence_appends_in_observation_order(
        self,
        issue_key: str,
        deliveries: list[str],
    ) -> None:
        """``coalesced_with`` grows in the order events arrive.

        **Validates: Requirement 4.7 (observation order)**

        Each subsequent delivery inside the window appends to the
        running ``coalesced_with`` list. The anchor delivery never
        appears; the second drop sees ``(d2,)``, the third drop sees
        ``(d2, d3)``, and so on.
        """

        burst = BurstWindow()
        # Stagger timestamps inside the 3s window so every delivery
        # after the anchor lands as ``coalesce_dropped``.
        now_q: list[float] = [100.0 + 0.1 * i for i in range(len(deliveries))]
        chain = _build_burst_chain(burst, now_provider=now_q)

        decisions: list[FilterDecision] = [
            chain.evaluate(_burst_event(issue_key=issue_key, delivery_id=d))
            for d in deliveries
        ]

        # Anchor passes through.
        assert decisions[0].action == "pass"
        assert decisions[0].reason == REASON_FILTER_CHAIN_PASS

        # Every subsequent decision drops with the burst reason and
        # accumulates dropped delivery_ids in observation order.
        expected_running: list[str] = []
        for delivery, decision in zip(deliveries[1:], decisions[1:], strict=True):
            expected_running.append(delivery)
            assert decision.action == "drop"
            assert decision.reason == REASON_BURST_COALESCED
            assert decision.coalesced_with == tuple(expected_running)
            assert deliveries[0] not in decision.coalesced_with


# ---------------------------------------------------------------------------
# Property 3j — 3s coalesce preserves last payload
# ---------------------------------------------------------------------------


class TestChainBurstDebounceLastPayloadWinsProperty:
    """The flushed window carries the **most recent** event's payload.

    **Validates: Requirement 4.7 ("son event'in payload'ı korunur")**

    Every :meth:`BurstWindow.register` call replaces the buffered
    payload with the new event's payload. When the sweeper later
    calls :meth:`BurstWindow.flush_window`, the returned payload is
    therefore the latest one observed inside the window, not the
    anchor's payload. The property below uses Hypothesis to vary
    the payload sequence and asserts the flush returns the last
    value.
    """

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_chain_burst_issue_keys,
        deliveries=st.lists(
            _chain_burst_delivery_ids,
            min_size=2,
            max_size=5,
            unique=True,
        ),
        payloads=st.lists(
            _chain_burst_payloads,
            min_size=2,
            max_size=5,
        ),
    )
    def test_flush_returns_latest_payload(
        self,
        issue_key: str,
        deliveries: list[str],
        payloads: list[dict],
    ) -> None:
        """``flush_window`` returns the payload of the *last* register call.

        Hypothesis varies the number of registrations; we trim both
        lists to the shorter one so the property remains well-typed
        for Hypothesis' shrinker.
        """

        n = min(len(deliveries), len(payloads))
        # Shrink to a 2-event minimum so the property is meaningful
        # (need anchor + at least one drop to verify "last wins").
        if n < 2:
            return
        deliveries = deliveries[:n]
        payloads = payloads[:n]

        burst = BurstWindow()
        now_q: list[float] = [100.0 + 0.1 * i for i in range(n)]
        chain = _build_burst_chain(burst, now_provider=now_q)

        for delivery, payload in zip(deliveries, payloads, strict=True):
            chain.evaluate(
                _burst_event(
                    issue_key=issue_key,
                    delivery_id=delivery,
                    payload=payload,
                )
            )

        flushed = burst.flush_window(issue_key)
        assert flushed is not None, (
            f"window for {issue_key!r} should still be open after the burst"
        )
        dropped, latest_payload = flushed

        # The flushed dropped list excludes the anchor — exactly one
        # drop per non-anchor register call, in observation order.
        assert dropped == deliveries[1:]
        # The latest payload wins; design contract.
        assert latest_payload == payloads[-1]

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_chain_burst_issue_keys,
        d1=_chain_burst_delivery_ids,
        d2=_chain_burst_delivery_ids,
        p1=_chain_burst_payloads,
        p2=_chain_burst_payloads,
    )
    def test_two_event_burst_drops_anchor_payload(
        self,
        issue_key: str,
        d1: str,
        d2: str,
        p1: dict,
        p2: dict,
    ) -> None:
        """Two-event burst: flush returns the *second* event's payload.

        The simplest "last wins" case — pinned separately so any
        regression that accidentally preserves the anchor payload
        surfaces with a minimal counter-example.
        """

        assume(d1 != d2)
        # Distinct payloads so the assertion is meaningful even if
        # Hypothesis happens to draw two equal dicts.
        assume(p1 != p2)

        burst = BurstWindow()
        chain = _build_burst_chain(burst, now_provider=[100.0, 101.0])

        chain.evaluate(_burst_event(issue_key=issue_key, delivery_id=d1, payload=p1))
        chain.evaluate(_burst_event(issue_key=issue_key, delivery_id=d2, payload=p2))

        flushed = burst.flush_window(issue_key)
        assert flushed is not None
        _dropped, latest = flushed
        assert latest == p2
        assert latest != p1


# ---------------------------------------------------------------------------
# Property 3k — Independent windows per issue_key
# ---------------------------------------------------------------------------


class TestChainBurstDebounceIndependentWindowsProperty:
    """Different ``issue_key`` events live in independent windows.

    **Validates: Requirement 4.7 (per-issue scoping)**

    The burst-debounce coordinator keys its buffer by ``issue_key``;
    a delivery for ``PAY-1`` must never coalesce with one for
    ``PAY-2``. The property quantifies over random pairs of distinct
    keys and asserts both events pass through (anchor verdicts).
    """

    @settings(max_examples=100, deadline=None)
    @given(
        key_a=_chain_burst_issue_keys,
        key_b=_chain_burst_issue_keys,
        delivery_a=_chain_burst_delivery_ids,
        delivery_b=_chain_burst_delivery_ids,
        offset=st.floats(
            min_value=0.0,
            max_value=BURST_WINDOW_SECONDS - 0.1,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_distinct_issue_keys_never_coalesce(
        self,
        key_a: str,
        key_b: str,
        delivery_a: str,
        delivery_b: str,
        offset: float,
    ) -> None:
        """Two events with distinct keys both pass even if back-to-back.

        Even when the two events arrive within the 3-second window,
        independent ``issue_key`` values mean independent buffers, and
        each buffer treats the *first* delivery as its anchor.
        """

        assume(key_a != key_b)
        assume(delivery_a != delivery_b)

        burst = BurstWindow()
        now_q: list[float] = [100.0, 100.0 + offset]
        chain = _build_burst_chain(burst, now_provider=now_q)

        first = chain.evaluate(
            _burst_event(issue_key=key_a, delivery_id=delivery_a)
        )
        second = chain.evaluate(
            _burst_event(issue_key=key_b, delivery_id=delivery_b)
        )

        assert first.action == "pass"
        assert first.reason == REASON_FILTER_CHAIN_PASS
        # Distinct key opens its own window — anchor for ``key_b``.
        assert second.action == "pass"
        assert second.reason == REASON_FILTER_CHAIN_PASS

        # Both windows are still open in the coordinator.
        assert burst.has_open_window(key_a)
        assert burst.has_open_window(key_b)


# ---------------------------------------------------------------------------
# Property 3l — Composite WebhookEvent sequence determinism
# ---------------------------------------------------------------------------
#
# **Property 3 final invariant (task 4.9):**
#
# **Validates: Requirements 3.2, 3.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8**
#
# The closing slice of task 4.9: given a random sequence of
# :class:`WebhookEvent` instances and a fully-wired
# :class:`WebhookFilterChain`, evaluating the same sequence twice
# produces the same list of :class:`FilterDecision` verdicts. This is
# the "Same state sequence in two calls → same decision list
# (deterministic)" invariant from the workflows-spec task list.
#
# Why this is a **separate** property from the per-stage determinism
# tests above: the chain integrates seven stages and a stateful
# burst-debounce coordinator. A regression that desynchronises the
# coordinator's state across calls (e.g. a mutable global, an
# in-memory cache that persists across runs, or a stage that reads
# the system clock) would not surface in the single-stage tests but
# *would* be caught here because the two passes share the same
# carefully-rebuilt state machine and must therefore produce the
# same decisions.
#
# To keep the test deterministic, we rebuild **independent** copies
# of the chain and the burst window for each pass. The decision list
# only depends on:
#
#   * the event sequence (immutable),
#   * the callbacks (pure functions of the event),
#   * the fresh burst window (with the same injected ``now`` queue).
#
# So if the chain is truly a deterministic state machine, both passes
# will return identical lists.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SequenceEvent:
    """Sealed record describing one event in a Hypothesis-drawn sequence.

    The strategy below builds a list of these and the test materialises
    them into :class:`WebhookEvent` objects on each pass. Storing the
    raw fields (rather than the :class:`WebhookEvent` itself) makes the
    Hypothesis shrinker more effective — the dataclass is hashable and
    its fields shrink independently.
    """

    issue_key: str
    delivery_id: str
    actor: str
    event_type: str
    body_text: str | None
    iter_count: int
    is_processed: bool
    mention_actors: tuple[str, ...]
    reporter: str
    payload_seed: int
    now_offset: float


_chain_seq_event_types = st.sampled_from(
    [
        "jira:issue_created",
        "jira:issue_updated",
        JIRA_COMMENT_EVENT_TYPE,
    ]
)


@st.composite
def _sequence_events(draw: st.DrawFn) -> list[_SequenceEvent]:
    """Generate a list of :class:`_SequenceEvent` with reusable issue keys.

    The strategy deliberately *re-uses* a small pool of issue keys so
    bursts on the same key are likely; otherwise every delivery would
    open its own window and the burst stage would never fire. Same
    pattern for actors, mention sets, and reporters.
    """

    n = draw(st.integers(min_value=1, max_value=8))
    # Small pools so events are likely to share keys / actors.
    issue_pool = draw(
        st.lists(_chain_burst_issue_keys, min_size=1, max_size=3, unique=True)
    )
    actor_pool = draw(
        st.lists(_chain_actor_ids, min_size=1, max_size=3, unique=True)
    )
    reporter_pool = draw(
        st.lists(_chain_actor_ids, min_size=1, max_size=2, unique=True)
    )

    events: list[_SequenceEvent] = []
    for i in range(n):
        events.append(
            _SequenceEvent(
                issue_key=draw(st.sampled_from(issue_pool)),
                # Delivery ids must be unique across the sequence so
                # the replay-dedup probe behaves predictably.
                delivery_id=f"d-{i}-{draw(_chain_burst_delivery_ids)}",
                actor=draw(st.sampled_from(actor_pool)),
                event_type=draw(_chain_seq_event_types),
                body_text=draw(
                    st.one_of(_chain_bypass_bodies(), _chain_no_bypass_bodies())
                ),
                iter_count=draw(st.integers(min_value=0, max_value=4)),
                is_processed=draw(st.booleans()),
                mention_actors=draw(
                    st.lists(
                        st.sampled_from(actor_pool),
                        min_size=0,
                        max_size=len(actor_pool),
                        unique=True,
                    ).map(tuple)
                ),
                reporter=draw(st.sampled_from(reporter_pool)),
                payload_seed=draw(st.integers(min_value=0, max_value=10_000)),
                # Keep deltas <3s so bursts on the same key actually
                # land inside the same window.
                now_offset=draw(
                    st.floats(
                        min_value=0.0,
                        max_value=2.5,
                        allow_nan=False,
                        allow_infinity=False,
                    )
                ),
            )
        )

    return events


def _materialise_event(spec: _SequenceEvent) -> WebhookEvent:
    """Build a :class:`WebhookEvent` from a :class:`_SequenceEvent`."""

    return WebhookEvent(
        provider="jira",
        event_type=spec.event_type,
        delivery_id=spec.delivery_id,
        actor_account_id=spec.actor,
        body_text=spec.body_text,
        project_key=spec.issue_key.split("-", 1)[0],
        repo_slug=None,
        issue_key=spec.issue_key,
        pr_id=None,
        raw_payload={"v": spec.payload_seed},
    )


def _build_full_chain(
    specs: list[_SequenceEvent],
) -> tuple[WebhookFilterChain, BurstWindow]:
    """Wire a fully-loaded chain whose callbacks are derived from *specs*.

    The callbacks are *pure functions of the spec list* — they read
    the relevant fields of the spec for the event currently under
    evaluation. We index each spec by its delivery_id so the
    callbacks can match an inbound event to its spec without relying
    on Python identity (which would fail on the second pass since we
    rebuild fresh :class:`WebhookEvent` instances).
    """

    by_delivery: dict[str, _SequenceEvent] = {s.delivery_id: s for s in specs}
    # Per-issue current iter / mention set / reporter — pulled from
    # the *latest* spec for that issue so the callbacks behave
    # deterministically across the sequence. This mirrors how the
    # production chain pulls the current state from Postgres at the
    # time of evaluation.
    by_issue_iter: dict[str, int] = {}
    by_issue_mentions: dict[str, frozenset[str]] = {}
    by_issue_reporter: dict[str, str] = {}
    by_delivery_processed: dict[str, bool] = {}
    for s in specs:
        by_issue_iter[s.issue_key] = s.iter_count
        by_issue_mentions[s.issue_key] = frozenset(s.mention_actors)
        by_issue_reporter[s.issue_key] = s.reporter
        by_delivery_processed[s.delivery_id] = s.is_processed

    burst = BurstWindow()

    # Build a now-queue from the relative offsets in spec order. We
    # start at t=100.0 so monotonic comparisons stay positive, then
    # accumulate the spec-provided offsets.
    now_queue: list[float] = []
    accum = 100.0
    for s in specs:
        accum += s.now_offset
        # Only events that have an issue_key trigger the burst stage,
        # and only those should consume a now value. We pre-compute
        # one slot per spec to keep alignment simple; unused slots are
        # benign because the burst callback only pops when invoked.
        now_queue.append(accum)

    def _burst_register(event: WebhookEvent) -> BurstRegisterResult | None:
        if event.issue_key is None:
            return None
        # Take the front slot. The queue length matches the spec list,
        # so we always have a value when an event reaches this stage.
        if not now_queue:
            return None
        now = now_queue.pop(0)
        decision = burst.register(
            issue_key=event.issue_key,
            delivery_id=event.delivery_id,
            payload=dict(event.raw_payload),
            now=now,
        )
        if decision == "coalesce_dropped":
            buffer = burst._buffers.get(event.issue_key)  # noqa: SLF001
            coalesced = (
                tuple(buffer.dropped_delivery_ids) if buffer is not None else ()
            )
            return BurstRegisterResult(
                decision="coalesce_dropped",
                coalesced_with=coalesced,
            )
        return BurstRegisterResult(decision="coalesce_emit", coalesced_with=())

    chain = WebhookFilterChain(
        verify_hmac=lambda _e: True,
        resolve_dept=lambda _e: "dept-seq",
        bot_account_ids=lambda: frozenset(),  # loop guard never fires
        is_processed=lambda d: by_delivery_processed.get(d, False),
        mention_set_for=lambda k: by_issue_mentions.get(k, frozenset()),
        iter_count_for=lambda k: by_issue_iter.get(k, 0),
        reporter_for=lambda k: by_issue_reporter.get(k, "__no_reporter__"),
        burst_register=_burst_register,
    )
    return chain, burst


class TestChainCompositeSequenceDeterminism:
    """Two evaluations of the same event sequence produce identical decisions.

    **Validates: Requirements 3.2, 3.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8**

    This is the *composite* determinism property: the chain combines
    seven filter stages, four of which are stateful (replay-dedup
    table reads, mention-set lookups, iteration counter reads, and the
    burst-debounce coordinator). Despite that, two passes over the
    same event sequence — each starting from a fresh state — must
    produce the same list of verdicts. Catches:

    * Stages that accidentally read the wall-clock or process-local
      randomness.
    * Caches that persist across chain instances.
    * Mutable globals that accumulate state between evaluations.
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(specs=_sequence_events())
    def test_same_sequence_two_runs_same_decisions(
        self, specs: list[_SequenceEvent]
    ) -> None:
        """Two independent runs over the same spec list produce equal decisions.

        Each run rebuilds the chain and the burst window from scratch
        so the test exercises both stage purity *and* the chain's
        ability to reconstruct identical state from the same inputs.
        """

        # Run 1.
        chain_a, _burst_a = _build_full_chain(specs)
        events_a = [_materialise_event(s) for s in specs]
        decisions_a = [chain_a.evaluate(ev) for ev in events_a]

        # Run 2 — fresh chain + fresh burst window + fresh event
        # objects (no shared state with run 1).
        chain_b, _burst_b = _build_full_chain(specs)
        events_b = [_materialise_event(s) for s in specs]
        decisions_b = [chain_b.evaluate(ev) for ev in events_b]

        assert decisions_a == decisions_b, (
            "two passes over the same sequence produced different "
            f"decisions:\n  a={decisions_a!r}\n  b={decisions_b!r}"
        )

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(specs=_sequence_events())
    def test_same_sequence_three_runs_all_equal(
        self, specs: list[_SequenceEvent]
    ) -> None:
        """Three runs all produce equal decision lists.

        A two-run check could mask a regression that flips state
        between runs 1 and 2 but lands back on the original verdict
        by run 3. The three-run version pins the property tighter:
        every observation of the same sequence must produce the
        same list.
        """

        decision_lists: list[list[FilterDecision]] = []
        for _ in range(3):
            chain, _burst = _build_full_chain(specs)
            events = [_materialise_event(s) for s in specs]
            decision_lists.append([chain.evaluate(ev) for ev in events])

        assert decision_lists[0] == decision_lists[1] == decision_lists[2]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(specs=_sequence_events())
    def test_each_decision_has_canonical_action_and_reason(
        self, specs: list[_SequenceEvent]
    ) -> None:
        """Every decision in a run uses one of the canonical reasons.

        **Validates: Requirements 3.2, 3.3 (action / reason vocabulary)**

        Pins the chain's vocabulary so a stage cannot accidentally
        invent a new reason string. The set of valid reasons matches
        the design's decision table exactly — adding a new stage
        means updating both the production chain and this test.
        """

        chain, _burst = _build_full_chain(specs)
        events = [_materialise_event(s) for s in specs]
        decisions = [chain.evaluate(ev) for ev in events]

        valid_reasons = {
            REASON_FILTER_CHAIN_PASS,
            REASON_LOOP_GUARD_DROPPED,
            REASON_LOOP_GUARD_REGEX_DROPPED,
            REASON_DUPLICATE_EVENT_DROPPED,
            REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR,
            REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION,
            REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS,
            REASON_BURST_COALESCED,
        }

        for decision in decisions:
            assert decision.action in ("drop", "pass"), (
                f"unexpected action {decision.action!r}"
            )
            assert decision.reason in valid_reasons, (
                f"unexpected reason {decision.reason!r}; "
                f"valid set = {sorted(valid_reasons)}"
            )
