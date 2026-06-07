"""Property tests for webhook loop-guard predicates.

Webhook predicate guards (loop, assignee, changelog, event-type).

The functions under test live in
``services/automation-service/src/decision/loop_guard.py``. They are pure
predicates over plain Python data (no I/O, no global state) which makes
them an ideal target for Hypothesis-driven property testing.

Invariants tested
-----------------

7a. ``is_self_actor(actor_id, bot_ids)`` returns ``True`` iff ``actor_id``
    is a member of ``bot_ids``; ``None`` actor always returns ``False``.

7b. ``is_bot_assignee(assignee_id, bot_ids)`` returns ``True`` iff
    ``assignee_id`` is a member of ``bot_ids``; ``None`` assignee always
    returns ``False``.

7c. ``assignee_changed_to_bot(changelog, bot_ids)`` returns ``True`` iff
    the changelog contains an item with ``field == "assignee"`` and
    ``to in bot_ids``. ``None`` changelog, empty ``items``, missing
    ``items``, and ``to is None`` all return ``False``.

7d. ``route(event_type)`` returns ``"accepted"`` for the supported event
    types and ``"ignored"`` for everything else. The return type is
    always one of those two literals.

7e. **Short-circuit ordering composition.** When the loop guard fires
    (``is_self_actor`` is True), the webhook handler skips the event; the
    later predicates (``is_bot_assignee``, ``assignee_changed_to_bot``,
    ``route``) are independent pure functions so their values cannot
    influence the loop-guard branch. We assert this independence by
    randomising the downstream inputs.

7f. **Determinism.** Every predicate is a pure function: repeated calls
    with the same arguments produce identical results.

This file mirrors the import / sys-path conventions used by the
sibling unit test ``tests/unit/test_loop_guard.py``: the
``automation-service/src`` directory is prepended to ``sys.path`` so the
``decision.loop_guard`` module imports without first ``pip install``-ing
the service.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# Ensure the automation-service ``src`` directory is importable when the
# tests are collected from any working directory (mirrors the pattern in
# ``tests/unit/test_loop_guard.py``).
_AUTOMATION_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_AUTOMATION_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SRC))

from decision.loop_guard import (  # noqa: E402  (sys.path bootstrap above)
    _ACCEPTED_EVENT_TYPES,
    assignee_changed_to_bot,
    is_bot_assignee,
    is_self_actor,
    route,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Atlassian-style account IDs: alphanumerics plus ``-`` / ``_``,
#: bounded length to keep examples small but realistic.
_account_ids: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=40,
)

#: A bot registry: a frozenset of 0-10 account IDs.
_bot_registries: st.SearchStrategy[frozenset[str]] = st.frozensets(
    _account_ids, min_size=0, max_size=10
)

#: A bot registry guaranteed to be non-empty (for tests that pick a bot).
_non_empty_bot_registries: st.SearchStrategy[frozenset[str]] = st.frozensets(
    _account_ids, min_size=1, max_size=10
)

#: ``actor_id``-shaped value that may be ``None``.
_optional_account_ids: st.SearchStrategy[str | None] = st.one_of(
    st.none(), _account_ids
)

#: All supported event types from the module under test, sampled
#: deterministically.
_supported_event_types: st.SearchStrategy[str] = st.sampled_from(
    sorted(_ACCEPTED_EVENT_TYPES)
)

#: Arbitrary event-type strings (may or may not be supported).
_arbitrary_event_types: st.SearchStrategy[str] = st.text(min_size=0, max_size=60)

#: The fields that may legitimately appear in a Jira changelog item.
_changelog_field_names: st.SearchStrategy[str] = st.sampled_from(
    ["assignee", "status", "priority", "summary", "description", "labels"]
)


@st.composite
def _changelog_item(draw: st.DrawFn) -> dict[str, object]:
    """Generate a single changelog item dict.

    The ``to`` value is drawn from a wide pool (None, an account-id-like
    string, or arbitrary text) so the property tests probe the full
    boundary surface of ``assignee_changed_to_bot``.
    """
    field = draw(_changelog_field_names)
    to_value = draw(
        st.one_of(st.none(), _account_ids, st.text(min_size=0, max_size=30))
    )
    from_value = draw(
        st.one_of(st.none(), _account_ids, st.text(min_size=0, max_size=30))
    )
    return {"field": field, "from": from_value, "to": to_value}


@st.composite
def _changelogs(draw: st.DrawFn) -> dict[str, list[dict[str, object]]] | None:
    """Generate a changelog dict, an empty changelog, or ``None``."""
    shape = draw(st.sampled_from(["none", "missing_items", "empty_items", "items"]))
    if shape == "none":
        return None
    if shape == "missing_items":
        return {}  # no ``items`` key at all
    if shape == "empty_items":
        return {"items": []}
    items = draw(st.lists(_changelog_item(), min_size=0, max_size=8))
    return {"items": items}


@st.composite
def _changelog_with_assignee_to_bot(
    draw: st.DrawFn, bot_ids: frozenset[str]
) -> dict[str, list[dict[str, object]]]:
    """Generate a changelog that *definitely* assigns to a bot.

    The bot id is sampled from the supplied non-empty registry. Other
    items are interspersed so the test exercises the linear scan logic
    instead of always seeing the assignee item first.
    """
    bot_id = draw(st.sampled_from(sorted(bot_ids)))
    other_items = draw(st.lists(_changelog_item(), min_size=0, max_size=5))
    assignee_item: dict[str, object] = {
        "field": "assignee",
        "from": draw(st.one_of(st.none(), _account_ids)),
        "to": bot_id,
    }
    position = draw(st.integers(min_value=0, max_value=len(other_items)))
    items = other_items[:position] + [assignee_item] + other_items[position:]
    return {"items": items}


# A standard Hypothesis ``settings`` profile for this module - bounded
# example count plus a generous deadline so CI on slow runners doesn't
# flake. The predicates are O(n) over the changelog so 200 examples
# completes well under a second locally.
_PROFILE = settings(
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# is_self_actor membership
# ---------------------------------------------------------------------------


class TestIsSelfActorMembership:
    """``is_self_actor`` is a pure membership test on the bot registry."""

    @_PROFILE
    @given(actor_id=_account_ids, bot_ids=_bot_registries)
    def test_actor_membership_iff_true(
        self, actor_id: str, bot_ids: frozenset[str]
    ) -> None:
        """For every actor and registry, the predicate's truth value
        equals set membership.
        """
        assert is_self_actor(actor_id, bot_ids) is (actor_id in bot_ids)

    @_PROFILE
    @given(bot_ids=_bot_registries)
    def test_none_actor_always_false(self, bot_ids: frozenset[str]) -> None:
        """``None`` actor (system-generated event) is never treated as a bot."""
        assert is_self_actor(None, bot_ids) is False

    @_PROFILE
    @given(actor_id=_optional_account_ids)
    def test_empty_registry_always_false(self, actor_id: str | None) -> None:
        """With no registered bots, no actor can be classified as one."""
        assert is_self_actor(actor_id, frozenset()) is False

    @_PROFILE
    @given(bot_ids=_non_empty_bot_registries)
    def test_known_bot_returns_true(self, bot_ids: frozenset[str]) -> None:
        """Sampling an actor from the registry must always classify True."""
        for bot in bot_ids:
            assert is_self_actor(bot, bot_ids) is True


# ---------------------------------------------------------------------------
# is_bot_assignee membership
# ---------------------------------------------------------------------------


class TestIsBotAssigneeMembership:
    """``is_bot_assignee`` is a pure membership test on the bot registry."""

    @_PROFILE
    @given(assignee_id=_account_ids, bot_ids=_bot_registries)
    def test_assignee_membership_iff_true(
        self, assignee_id: str, bot_ids: frozenset[str]
    ) -> None:
        """Assignee membership in the bot registry returns true."""
        assert is_bot_assignee(assignee_id, bot_ids) is (assignee_id in bot_ids)

    @_PROFILE
    @given(bot_ids=_bot_registries)
    def test_none_assignee_always_false(self, bot_ids: frozenset[str]) -> None:
        """Unassigned issue (``None`` assignee) is never a bot."""
        assert is_bot_assignee(None, bot_ids) is False

    @_PROFILE
    @given(assignee_id=_optional_account_ids)
    def test_empty_registry_always_false(
        self, assignee_id: str | None
    ) -> None:
        """An empty registry never matches an assignee."""
        assert is_bot_assignee(assignee_id, frozenset()) is False

    @_PROFILE
    @given(actor_id=_optional_account_ids, bot_ids=_bot_registries)
    def test_agrees_with_is_self_actor_on_same_inputs(
        self, actor_id: str | None, bot_ids: frozenset[str]
    ) -> None:
        """The two membership predicates have identical semantics; only the
        operational role of the input differs (actor vs. assignee).
        """
        assert is_self_actor(actor_id, bot_ids) is is_bot_assignee(
            actor_id, bot_ids
        )


# ---------------------------------------------------------------------------
# assignee_changed_to_bot changelog invariants
# ---------------------------------------------------------------------------


class TestAssigneeChangedToBot:
    """Changelog scanning invariants for ``assignee_changed_to_bot``."""

    @_PROFILE
    @given(data=st.data(), bot_ids=_non_empty_bot_registries)
    def test_changelog_assigning_to_bot_returns_true(
        self, data: st.DataObject, bot_ids: frozenset[str]
    ) -> None:
        """Any changelog containing a single ``assignee  bot`` item, even
        among unrelated noise items, must be flagged.
        """
        changelog = data.draw(_changelog_with_assignee_to_bot(bot_ids))
        assert assignee_changed_to_bot(changelog, bot_ids) is True

    @_PROFILE
    @given(bot_ids=_bot_registries)
    def test_none_changelog_always_false(self, bot_ids: frozenset[str]) -> None:
        """A missing changelog never indicates an assignee change to a bot."""
        assert assignee_changed_to_bot(None, bot_ids) is False

    @_PROFILE
    @given(bot_ids=_bot_registries)
    def test_missing_items_key_always_false(
        self, bot_ids: frozenset[str]
    ) -> None:
        """A missing ``items`` key never indicates an assignee change to a bot."""
        assert assignee_changed_to_bot({}, bot_ids) is False

    @_PROFILE
    @given(bot_ids=_bot_registries)
    def test_empty_items_list_always_false(
        self, bot_ids: frozenset[str]
    ) -> None:
        """An empty ``items`` list never indicates an assignee change to a bot."""
        assert assignee_changed_to_bot({"items": []}, bot_ids) is False

    @_PROFILE
    @given(bot_ids=_bot_registries)
    def test_assignee_removed_to_none_returns_false(
        self, bot_ids: frozenset[str]
    ) -> None:
        """Assignee removal (``to`` is ``None``) is not a bot assignment,
        even when bots exist in the registry.
        """
        changelog = {
            "items": [{"field": "assignee", "from": "some-user", "to": None}]
        }
        assert assignee_changed_to_bot(changelog, bot_ids) is False

    @_PROFILE
    @given(non_bot_id=_account_ids, bot_ids=_bot_registries)
    def test_assignee_changed_to_non_bot_returns_false(
        self, non_bot_id: str, bot_ids: frozenset[str]
    ) -> None:
        """Changing assignee to a non-bot does not trigger the predicate."""
        assume(non_bot_id not in bot_ids)
        changelog = {
            "items": [
                {"field": "assignee", "from": "someone", "to": non_bot_id}
            ]
        }
        assert assignee_changed_to_bot(changelog, bot_ids) is False

    @_PROFILE
    @given(
        bot_ids=_bot_registries,
        items=st.lists(
            _changelog_item().filter(lambda it: it["field"] != "assignee"),
            min_size=0,
            max_size=8,
        ),
    )
    def test_no_assignee_field_returns_false(
        self,
        bot_ids: frozenset[str],
        items: list[dict[str, object]],
    ) -> None:
        """A changelog whose items never reference the ``assignee`` field
        cannot trigger the predicate, regardless of registry contents.
        """
        assert assignee_changed_to_bot({"items": items}, bot_ids) is False

    @_PROFILE
    @given(changelog=_changelogs(), bot_ids=_bot_registries)
    def test_implies_some_item_has_assignee_to_bot(
        self,
        changelog: dict | None,
        bot_ids: frozenset[str],
    ) -> None:
        """``True`` from the predicate implies that the changelog actually
        contains a matching item - i.e. there are no false positives.
        """
        if assignee_changed_to_bot(changelog, bot_ids):
            assert changelog is not None
            items = changelog.get("items") or []
            matching = [
                it
                for it in items
                if isinstance(it, dict)
                and it.get("field") == "assignee"
                and it.get("to") in bot_ids
            ]
            assert matching, (
                "predicate returned True but no item matched: "
                f"changelog={changelog!r}, bot_ids={sorted(bot_ids)!r}"
            )


# ---------------------------------------------------------------------------
# route event-type classifier
# ---------------------------------------------------------------------------


class TestRouteEventType:
    """``route`` partitions event-type strings into accepted vs ignored."""

    @_PROFILE
    @given(event_type=_supported_event_types)
    def test_supported_event_types_are_accepted(self, event_type: str) -> None:
        """Supported event types are accepted."""
        assert route(event_type) == "accepted"

    @_PROFILE
    @given(event_type=_arbitrary_event_types)
    def test_unsupported_event_types_are_ignored(
        self, event_type: str
    ) -> None:
        """Any string not in the explicit supported set is ignored."""
        assume(event_type not in _ACCEPTED_EVENT_TYPES)
        assert route(event_type) == "ignored"

    @_PROFILE
    @given(event_type=_arbitrary_event_types)
    def test_return_value_is_one_of_two_literals(
        self, event_type: str
    ) -> None:
        """``route`` is total: the codomain is exactly
        ``{"accepted", "ignored"}``.
        """
        assert route(event_type) in ("accepted", "ignored")

    def test_required_event_type_set_is_exhaustive(self) -> None:
        """The platform must support these specific Jira and Bitbucket event types."""
        required = {
            "jira:issue_created",
            "jira:issue_assigned",
            "jira:issue_updated",
            "jira:comment_created",
            "pullrequest:reviewer_added",
            "pullrequest:comment_created",
        }
        for event_type in required:
            assert route(event_type) == "accepted", (
                f"required event type {event_type!r} is not accepted"
            )


# ---------------------------------------------------------------------------
# short-circuit ordering composition
# ---------------------------------------------------------------------------


class TestShortCircuitOrdering:
    """The webhook handler evaluates predicates in a fixed sequence:

        1. ``is_self_actor``   if True, skip immediately (loop guard)
        2. ``is_bot_assignee`` / ``assignee_changed_to_bot`` (per event)
        3. ``route``            classifies the event type

    Step 1 short-circuits steps 2-3. Because each predicate is a pure
    function, the *value* of a downstream predicate is a function of
    its own inputs alone - varying those inputs cannot retroactively
    flip the loop guard's verdict. The properties below assert this
    independence directly.
    """

    @_PROFILE
    @given(
        bot_ids=_non_empty_bot_registries,
        downstream_assignee=_optional_account_ids,
        changelog=_changelogs(),
        event_type=_arbitrary_event_types,
    )
    def test_self_actor_decision_independent_of_downstream_inputs(
        self,
        bot_ids: frozenset[str],
        downstream_assignee: str | None,
        changelog: dict | None,
        event_type: str,
    ) -> None:
        """When the actor *is* a registered bot, ``is_self_actor`` returns
        ``True`` no matter what downstream inputs (assignee, changelog,
        event type) the webhook carries. This is the formal statement
        of "step 1 short-circuits steps 2-3".
        """
        bot_actor = sorted(bot_ids)[0]
        # Step 1 fires regardless of what the rest of the payload says.
        assert is_self_actor(bot_actor, bot_ids) is True

        # Downstream predicates remain *callable* and *deterministic* -
        # they simply do not influence the loop-guard branch the
        # handler took.
        _ = is_bot_assignee(downstream_assignee, bot_ids)
        _ = assignee_changed_to_bot(changelog, bot_ids)
        _ = route(event_type)

        # And step 1 still fires after we exercised the downstream
        # predicates: no hidden mutable state could have flipped it.
        assert is_self_actor(bot_actor, bot_ids) is True

    @_PROFILE
    @given(
        actor_id=_account_ids,
        bot_ids=_bot_registries,
        event_type=_supported_event_types,
    )
    def test_non_bot_actor_does_not_short_circuit(
        self,
        actor_id: str,
        bot_ids: frozenset[str],
        event_type: str,
    ) -> None:
        """When the actor is *not* a bot, the loop guard does not fire
        and the routing predicate becomes the next decision point.
        """
        assume(actor_id not in bot_ids)
        assert is_self_actor(actor_id, bot_ids) is False
        # Downstream classification is now meaningful - it must be one
        # of the two literals route() can return.
        assert route(event_type) in ("accepted", "ignored")

    @_PROFILE
    @given(bot_ids=_non_empty_bot_registries)
    def test_loop_guard_dominates_assignee_check(
        self, bot_ids: frozenset[str]
    ) -> None:
        """Even when the bot is *also* the assignee, the loop guard's
        decision still applies first: the event is skipped because
        ``is_self_actor`` already returned True.

        We assert the composition by checking that both predicates
        return True on the same bot id - but the handler's contract is
        that the handler stops after step 1, never reaching step 2.
        """
        bot_id = sorted(bot_ids)[0]
        assert is_self_actor(bot_id, bot_ids) is True
        # is_bot_assignee would also fire, but step 1 already decided.
        assert is_bot_assignee(bot_id, bot_ids) is True


# ---------------------------------------------------------------------------
# determinism (purity)
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Every predicate is a pure function: same inputs  same output."""

    @_PROFILE
    @given(actor_id=_optional_account_ids, bot_ids=_bot_registries)
    def test_is_self_actor_deterministic(
        self, actor_id: str | None, bot_ids: frozenset[str]
    ) -> None:
        """``is_self_actor`` is deterministic."""
        r1 = is_self_actor(actor_id, bot_ids)
        r2 = is_self_actor(actor_id, bot_ids)
        r3 = is_self_actor(actor_id, bot_ids)
        assert r1 is r2 is r3

    @_PROFILE
    @given(assignee_id=_optional_account_ids, bot_ids=_bot_registries)
    def test_is_bot_assignee_deterministic(
        self, assignee_id: str | None, bot_ids: frozenset[str]
    ) -> None:
        """``is_bot_assignee`` is deterministic."""
        r1 = is_bot_assignee(assignee_id, bot_ids)
        r2 = is_bot_assignee(assignee_id, bot_ids)
        r3 = is_bot_assignee(assignee_id, bot_ids)
        assert r1 is r2 is r3

    @_PROFILE
    @given(changelog=_changelogs(), bot_ids=_bot_registries)
    def test_assignee_changed_to_bot_deterministic(
        self, changelog: dict | None, bot_ids: frozenset[str]
    ) -> None:
        """``assignee_changed_to_bot`` is deterministic."""
        r1 = assignee_changed_to_bot(changelog, bot_ids)
        r2 = assignee_changed_to_bot(changelog, bot_ids)
        r3 = assignee_changed_to_bot(changelog, bot_ids)
        assert r1 is r2 is r3

    @_PROFILE
    @given(event_type=_arbitrary_event_types)
    def test_route_deterministic(self, event_type: str) -> None:
        """``route`` is deterministic."""
        r1 = route(event_type)
        r2 = route(event_type)
        r3 = route(event_type)
        assert r1 == r2 == r3
