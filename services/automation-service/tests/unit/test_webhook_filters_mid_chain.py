"""Unit tests for mid-chain stages of :class:`WebhookFilterChain`.

This module pins the behaviour of the filter-chain stages that run
between the verifier stages and the burst-debounce stage:

* ``_stage_streamlit_bypass`` handles the ``[bot:hear]`` etiquette
  tag bypass. Returns ``FilterDecision(action="pass",
  reason="streamlit_inline_reply_with_bypass")`` and short-circuits
  the rest of the chain so retries of Streamlit inline replies are
  honoured.
* ``_stage_replay_dedup`` handles idempotency.
  Returns ``FilterDecision(action="drop",
  reason="duplicate_event_dropped")`` when ``is_processed(delivery_id)``
  is ``True``.
* ``_stage_mention_filter`` drops ``jira:issue_commented`` events
  whose actor is not in the bot-mentioned set when ``iter_count > 1``,
  and bypasses with ``mention_filter_first_iter_exception`` when
  ``iter_count == 1`` and the actor matches the issue reporter.

The companion stages (``verify_hmac``, ``resolve_dept``,
``loop_guard``) are covered by the sibling file
``test_webhook_filter_stages.py``; this module deliberately does **not**
overlap with that coverage. The composite chain-level invariants
(precedence, determinism over random sequences) live in the property
suite at ``platform/tests/property/test_webhook_predicates.py``.

Every test class targets exactly one stage so a stage-local regression
shrinks the failing example to that stage's logic without dragging in
unrelated callbacks. The composite stage-ordering tests live at the
bottom; their purpose is to pin the
``streamlit_bypass → replay_dedup → mention_filter`` precedence so the
stage-level suites above can stay focused on individual decision
tables.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Callable

import pytest

# Ensure the automation-service ``src`` directory is importable when
# the tests are collected from any working directory. Two ``sys.path``
# entries are required because importing ``automation_service`` lands
# in ``automation_service.__init__`` which eagerly loads
# ``automation_service.app`` whose top-of-module imports reach for the
# legacy ``from src.config import Settings`` re-export. Mirrors the
# bootstrap used by ``test_webhook_filter_stages.py`` so this module
# stays parallel-collectable next to its sibling.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

from automation_service.webhook_filters import (  # noqa: E402
    JIRA_COMMENT_EVENT_TYPE,
    REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR,
    REASON_DUPLICATE_EVENT_DROPPED,
    REASON_FILTER_CHAIN_PASS,
    REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION,
    REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS,
    STREAMLIT_BYPASS_TAG,
    FilterDecision,
    WebhookEvent,
    WebhookFilterChain,
)


# ---------------------------------------------------------------------------
# Helpers - minimal callback set for chains under test
# ---------------------------------------------------------------------------


def _make_chain(
    *,
    verify_hmac: Callable[[WebhookEvent], bool] = lambda ev: True,
    resolve_dept: Callable[[WebhookEvent], str | None] = lambda ev: "payments",
    bot_account_ids: Callable[[], frozenset[str]] = lambda: frozenset(),
    is_processed: Callable[[str], bool] = lambda d: False,
    mention_set_for: Callable[[str], frozenset[str]] = lambda i: frozenset(),
    iter_count_for: Callable[[str], int] = lambda i: 0,
    reporter_for: Callable[[str], str] = lambda i: "reporter-1",
    burst_window: timedelta = timedelta(seconds=3),
) -> WebhookFilterChain:
    """Build a chain wired to deterministic callbacks for unit tests.

    Defaults model a healthy webhook delivery: HMAC verifies, dept
    resolves to ``"payments"``, no bots registered, no replay history,
    no mentions, iter 0, reporter ``"reporter-1"``. Each test
    overrides only the callback it cares about.
    """

    return WebhookFilterChain(
        verify_hmac=verify_hmac,
        resolve_dept=resolve_dept,
        bot_account_ids=bot_account_ids,
        is_processed=is_processed,
        mention_set_for=mention_set_for,
        iter_count_for=iter_count_for,
        reporter_for=reporter_for,
        burst_window=burst_window,
    )


def _make_comment_event(
    *,
    delivery_id: str = "delivery-1",
    actor_account_id: str | None = "human-user",
    body_text: str | None = "a regular comment",
    issue_key: str | None = "PAY-1",
) -> WebhookEvent:
    """Build a normalised ``jira:issue_commented`` event.

    The mention-filter / first-iter-exception stages are scoped to
    Jira comment events, so the helper hardcodes that ``event_type``.
    Callers exercising the streamlit-bypass / replay-dedup stages can
    pass non-comment ``event_type`` overrides through
    :func:`_make_event` instead.
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


def _make_event(
    *,
    provider: str = "jira",
    event_type: str = "jira:issue_created",
    delivery_id: str = "delivery-1",
    actor_account_id: str | None = "human-user",
    body_text: str | None = None,
    project_key: str | None = "PAY",
    repo_slug: str | None = None,
    issue_key: str | None = "PAY-1",
    pr_id: int | None = None,
) -> WebhookEvent:
    """Build a generic :class:`WebhookEvent` with safe Jira defaults.

    Mirrors the helper in ``test_webhook_filter_stages.py`` so the two
    test modules can co-exist without diverging fixture shapes.
    """

    return WebhookEvent(
        provider=provider,  # type: ignore[arg-type]
        event_type=event_type,
        delivery_id=delivery_id,
        actor_account_id=actor_account_id,
        body_text=body_text,
        project_key=project_key,
        repo_slug=repo_slug,
        issue_key=issue_key,
        pr_id=pr_id,
    )


# ---------------------------------------------------------------------------
# streamlit_bypass stage
# ---------------------------------------------------------------------------


class TestStreamlitBypassStage:
    """``_stage_streamlit_bypass`` short-circuits when ``[bot:hear]`` is present."""

    def test_body_with_bot_hear_passes_with_bypass_reason(self) -> None:
        """``[bot:hear]`` in the body → pass with the canonical reason.

        """

        chain = _make_chain()
        event = _make_event(
            event_type=JIRA_COMMENT_EVENT_TYPE,
            body_text="[bot:hear] please regenerate the diff summary",
        )

        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS
        assert (
            REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS
            == "streamlit_inline_reply_with_bypass"
        )

    @pytest.mark.parametrize(
        "body",
        [
            "[bot:hear] regenerate",
            "[BOT:HEAR] regenerate",
            "[Bot:Hear] regenerate",
            "[bOt:HeAr] mixed casing",
            "  [bot:hear]  surrounded by whitespace",
            "see prior context [bot:hear] in the middle of a sentence",
            "trailing context [bot:hear]",
        ],
    )
    def test_bypass_tag_match_is_case_insensitive_and_anywhere(
        self, body: str
    ) -> None:
        """The tag may live anywhere in the body and casing is ignored.

        """

        chain = _make_chain()
        event = _make_event(
            event_type=JIRA_COMMENT_EVENT_TYPE,
            body_text=body,
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS

    @pytest.mark.parametrize(
        "body",
        [
            None,
            "",
            "this is just a normal user comment",
            "the bot already wrote [bot:hear-no-evil] earlier",  # different tag
            "[bot:speak] this is a different tag",
            "bot:hear without the brackets",
        ],
    )
    def test_body_without_bypass_tag_falls_through(
        self, body: str | None
    ) -> None:
        """A body without the ``[bot:hear]`` tag falls through.

        With every other callback set to no-op pass-through, the
        chain reaches the default ``filter_chain_pass`` verdict.

        """

        chain = _make_chain()
        event = _make_event(
            event_type=JIRA_COMMENT_EVENT_TYPE,
            body_text=body,
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS

    def test_bypass_tag_constant_shape(self) -> None:
        """The exported :data:`STREAMLIT_BYPASS_TAG` is the design's literal.

        Pins the literal so a typo (``[bot: hear]``, ``[bot-hear]``)
        in the constant surfaces as a test diff. Mirrors the
        ``BOT_PREFIX_REGEX`` shape pin in ``test_webhook_filter_stages.py``.

        """

        assert STREAMLIT_BYPASS_TAG == "[bot:hear]"

    def test_bypass_takes_precedence_over_replay_dedup(self) -> None:
        """A ``[bot:hear]`` retry is honoured even when the delivery is seen.

        The whole point of running streamlit_bypass before replay_dedup
        is so the user's inline reply survives Atlassian's at-least-once
        delivery semantics. We pin the precedence by making
        ``is_processed`` return True; the chain must still pass the
        event through with the bypass reason.

        """

        chain = _make_chain(is_processed=lambda d: True)
        event = _make_event(
            event_type=JIRA_COMMENT_EVENT_TYPE,
            body_text="[bot:hear] retry",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS

    def test_bypass_takes_precedence_over_mention_filter(self) -> None:
        """A ``[bot:hear]`` reply skips the mention filter even at iter > 1.

        Without the bypass, an unauthorised commenter at iter > 1
        would be dropped with ``comment_ignored_unauthorized_actor``.
        The bypass tag is what lets the Streamlit UI proxy a comment
        on the user's behalf - it must short-circuit Y6.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 5,
            mention_set_for=lambda i: frozenset(),  # no one is mentioned
        )
        event = _make_comment_event(
            actor_account_id="random-third-party",
            body_text="[bot:hear] please address my concern",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS


class TestStreamlitBypassTagDetection:
    """``WebhookFilterChain._has_streamlit_bypass_tag`` is exposed for re-use.

    The static helper lets the FastAPI router and ad-hoc
    debugging tooling check the tag without instantiating a chain.
    These tests pin its contract.

    """

    @pytest.mark.parametrize(
        "body, expected",
        [
            (None, False),
            ("", False),
            ("[bot:hear]", True),
            ("[BOT:HEAR]", True),
            ("  [bot:hear]  ", True),
            ("hi [bot:hear] there", True),
            ("hi", False),
            ("[bot:speak]", False),
        ],
    )
    def test_static_helper_matches_design(
        self, body: str | None, expected: bool
    ) -> None:
        """Cover the truth table of :meth:`_has_streamlit_bypass_tag`."""

        assert (
            WebhookFilterChain._has_streamlit_bypass_tag(body) is expected
        )


# ---------------------------------------------------------------------------
# replay_dedup stage
# ---------------------------------------------------------------------------


class TestReplayDedupStage:
    """``_stage_replay_dedup`` drops events whose ``delivery_id`` was seen."""

    def test_already_processed_drops_with_duplicate_event_dropped(self) -> None:
        """``is_processed`` True → drop with the canonical reason.

        """

        chain = _make_chain(is_processed=lambda d: True)
        event = _make_event(delivery_id="seen-before")

        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "drop"
        assert decision.reason == REASON_DUPLICATE_EVENT_DROPPED
        assert REASON_DUPLICATE_EVENT_DROPPED == "duplicate_event_dropped"

    def test_unseen_delivery_falls_through(self) -> None:
        """``is_processed`` False → fall through to the next stage.

        With every other callback set to no-op the chain reaches
        ``filter_chain_pass``.

        """

        chain = _make_chain(is_processed=lambda d: False)
        event = _make_event(delivery_id="fresh-delivery")

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS

    def test_is_processed_called_with_raw_delivery_id(self) -> None:
        """The chain forwards ``delivery_id`` verbatim.

        The callback is responsible for choosing the canonical
        idempotency key (``X-Atlassian-Webhook-Identifier`` for Jira,
        ``X-Request-UUID`` for Bitbucket); the chain MUST NOT hash,
        normalise, or mutate the value.

        """

        captured: list[str] = []

        def _is_processed(delivery_id: str) -> bool:
            captured.append(delivery_id)
            return False

        chain = _make_chain(is_processed=_is_processed)
        event = _make_event(delivery_id="raw-delivery-id-with-special-chars/=+")

        chain.evaluate(event)

        assert captured == ["raw-delivery-id-with-special-chars/=+"]

    def test_replay_dedup_runs_after_streamlit_bypass(self) -> None:
        """A ``[bot:hear]`` retry survives even when ``is_processed`` is True.

        Mirrors :meth:`TestStreamlitBypassStage.test_bypass_takes_precedence_over_replay_dedup`
        from the dedup-side: we expose the same invariant as a
        replay-dedup-stage property so both reading orientations
        catch a regression in the precedence wiring.

        """

        chain = _make_chain(is_processed=lambda d: True)
        event = _make_event(
            event_type=JIRA_COMMENT_EVENT_TYPE,
            body_text=" [bot:hear]  retry from streamlit",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS

    def test_replay_dedup_short_circuits_mention_filter(self) -> None:
        """A duplicate event drops before the mention-filter stage runs.

        We assert short-circuit by counting calls into the
        ``iter_count_for`` callback - the mention-filter stage is
        the first stage past replay-dedup that consults it. A
        duplicate-dropped event must never reach the mention filter.

        """

        iter_calls = 0

        def _iter_count(_issue: str) -> int:
            nonlocal iter_calls
            iter_calls += 1
            return 5

        chain = _make_chain(
            is_processed=lambda d: True,
            iter_count_for=_iter_count,
        )
        event = _make_comment_event(delivery_id="dup")

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_DUPLICATE_EVENT_DROPPED
        assert iter_calls == 0


# ---------------------------------------------------------------------------
# mention_filter stage
# ---------------------------------------------------------------------------


class TestMentionFilterStage:
    """``_stage_mention_filter`` enforces Y6 for ``jira:issue_commented`` events.

    Only fires when ``event_type == jira:issue_commented`` and
    ``iter_count > 1``. Drops with ``comment_ignored_unauthorized_actor``
    when the actor is not in the bot-mentioned set for the issue.
    """

    def test_unauthorized_actor_at_iter_two_drops(self) -> None:
        """iter > 1 + actor ∉ mention_set → drop with Y6 reason.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 2,
            mention_set_for=lambda i: frozenset({"alice", "bob"}),
        )
        event = _make_comment_event(actor_account_id="random-third-party")

        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "drop"
        assert decision.reason == REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR
        assert (
            REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR
            == "comment_ignored_unauthorized_actor"
        )

    def test_mentioned_actor_at_iter_two_passes(self) -> None:
        """iter > 1 + actor ∈ mention_set → pass through.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 2,
            mention_set_for=lambda i: frozenset({"alice", "bob"}),
        )
        event = _make_comment_event(actor_account_id="alice")

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS

    @pytest.mark.parametrize("iter_count", [2, 3, 5, 10, 100])
    def test_y6_fires_for_every_iter_above_one(self, iter_count: int) -> None:
        """Y6 enforcement holds for any ``iter_count > 1``.

        """

        chain = _make_chain(
            iter_count_for=lambda i: iter_count,
            mention_set_for=lambda i: frozenset({"alice"}),
        )
        event = _make_comment_event(actor_account_id="random-third-party")

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR

    def test_none_actor_at_iter_two_drops(self) -> None:
        """iter > 1 + actor None → drop (no actor cannot be mentioned).

        The Y6 predicate's "actor not in mention_set" branch fires
        when the actor is missing entirely - we cannot match a
        ``None`` against any account id, so the comment is treated
        as unauthorised.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 3,
            mention_set_for=lambda i: frozenset({"alice"}),
        )
        event = _make_comment_event(actor_account_id=None)

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR

    def test_non_comment_event_bypasses_mention_filter(self) -> None:
        """Only ``jira:issue_commented`` is in scope; other events pass.

        ``jira:issue_created``, ``pullrequest:created`` etc. must
        flow through the mention-filter stage unchanged regardless
        of iter / mention-set state.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 5,  # would trigger Y6 if scope mismatched
            mention_set_for=lambda i: frozenset({"alice"}),
        )
        # ``jira:issue_created`` - out of Y6 scope.
        event = _make_event(
            event_type="jira:issue_created",
            actor_account_id="random-third-party",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS

    def test_missing_issue_key_passes_through_defensively(self) -> None:
        """Comment events without ``issue_key`` cannot be evaluated → pass.

        Real Atlassian comment payloads always populate ``issue.key``;
        if an upstream contract change ever lands an event without
        one, the chain conservatively lets it flow through rather
        than dropping it on a callback-shape mismatch. The FastAPI
        router validates the schema, so this is a defensive guard
        not a hot-path expectation.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 5,
            mention_set_for=lambda i: frozenset(),
        )
        event = _make_comment_event(
            actor_account_id="random-third-party",
            issue_key=None,
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS

    def test_iter_count_callback_keyed_on_issue_key(self) -> None:
        """The chain calls ``iter_count_for`` with the event's issue_key.

        Pins the callback contract so a future refactor that drops
        the issue_key argument surfaces as a test failure. The
        integration tests rely on this argument shape to scope iter
        counts to the right Jira issue.

        """

        captured: list[str] = []

        def _iter_count(issue_key: str) -> int:
            captured.append(issue_key)
            return 2

        chain = _make_chain(
            iter_count_for=_iter_count,
            mention_set_for=lambda i: frozenset(),
        )
        event = _make_comment_event(
            issue_key="PAY-9999",
            actor_account_id="random",
        )

        chain.evaluate(event)

        assert captured == ["PAY-9999"]

    def test_mention_set_callback_keyed_on_issue_key(self) -> None:
        """The chain calls ``mention_set_for`` with the event's issue_key.

        Pins the same callback-argument contract as the iter test.
        The mention set must be issue-scoped so a comment on PAY-1
        does not leak the mention set from PAY-2.

        """

        captured: list[str] = []

        def _mention_set(issue_key: str) -> frozenset[str]:
            captured.append(issue_key)
            return frozenset()

        chain = _make_chain(
            iter_count_for=lambda i: 2,
            mention_set_for=_mention_set,
        )
        event = _make_comment_event(
            issue_key="PAY-9999",
            actor_account_id="random",
        )

        chain.evaluate(event)

        assert captured == ["PAY-9999"]


# ---------------------------------------------------------------------------
# first_iter_exception
# ---------------------------------------------------------------------------


class TestMentionFilterFirstIterException:
    """``_stage_mention_filter`` honours Z6 for iter == 1 + reporter actors.

    Z6 lets the issue reporter trigger the bot on the very first
    iteration without first having to be mentioned by the bot -
    which would be impossible because the bot has not yet commented.
    The exception is folded into the mention-filter stage so its
    precedence over Y6 is structural rather than wiring-dependent.
    """

    def test_iter_one_reporter_passes_with_first_iter_reason(self) -> None:
        """iter == 1 + actor == reporter → bypass with the Z6 audit reason.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 1,
            reporter_for=lambda i: "alice",
            mention_set_for=lambda i: frozenset(),  # nobody mentioned yet
        )
        event = _make_comment_event(actor_account_id="alice")

        decision = chain.evaluate(event)

        assert decision.action == "pass"
        assert decision.reason == REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION
        assert (
            REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION
            == "mention_filter_first_iter_exception"
        )

    def test_iter_one_non_reporter_falls_through_without_z6(self) -> None:
        """iter == 1 + actor != reporter → fall through (no Z6 label).

        Z6 specifically requires actor == reporter. Other commenters
        at iter 1 still pass (Y6 only enforces from iter 2 onward),
        but they should NOT get the ``mention_filter_first_iter_exception``
        audit reason - that label is reserved for the reporter.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 1,
            reporter_for=lambda i: "alice",
            mention_set_for=lambda i: frozenset(),
        )
        event = _make_comment_event(actor_account_id="bob-different-from-reporter")

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS

    def test_iter_two_reporter_no_longer_qualifies_for_z6(self) -> None:
        """iter > 1 + actor == reporter → no Z6 (Z6 is iter-1 only).

        At iter 2 the reporter must already be in the mention set
        like everyone else (typically true because the bot mentioned
        the reporter in its iter-1 reply). Falling out of Z6 is what
        forces the mention-set discipline going forward.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 2,
            reporter_for=lambda i: "alice",
            mention_set_for=lambda i: frozenset({"alice", "bob"}),
        )
        event = _make_comment_event(actor_account_id="alice")

        decision = chain.evaluate(event)
        # Reporter is also in mention set at iter 2 → pass with the
        # default ``filter_chain_pass`` reason, NOT Z6.
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS

    def test_iter_zero_treated_as_iter_one_for_z6(self) -> None:
        """iter == 0 + actor == reporter → Z6 fires.

        The design's iter semantics treat ``0`` as "freshly created
        issue, no iter advanced yet" and the bot's first reply will
        bump it to 1; but the reporter's first comment may arrive
        before that bump happens. The chain therefore treats ``0``
        identically to ``1`` for Z6 purposes so the reporter is not
        accidentally locked out by the race.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 0,
            reporter_for=lambda i: "alice",
            mention_set_for=lambda i: frozenset(),
        )
        event = _make_comment_event(actor_account_id="alice")

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION

    def test_z6_dominates_y6_when_actor_also_in_mention_set(self) -> None:
        """Z6 produces the stable label even when Y6 would also pass.

        At iter 1 with the reporter in the mention set, both Z6 and
        the implicit "iter <= 1 has no Y6 enforcement" path would
        let the event through. The chain audit-labels the verdict
        as Z6 so operators get a single, stable label per condition.

        """

        chain = _make_chain(
            iter_count_for=lambda i: 1,
            reporter_for=lambda i: "alice",
            mention_set_for=lambda i: frozenset({"alice"}),
        )
        event = _make_comment_event(actor_account_id="alice")

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION

    def test_reporter_for_callback_keyed_on_issue_key(self) -> None:
        """The chain calls ``reporter_for`` with the event's issue_key.

        Pins the callback contract; the runtime implementation reads
        the reporter id from Postgres / the Jira API on demand.

        """

        captured: list[str] = []

        def _reporter(issue_key: str) -> str:
            captured.append(issue_key)
            return "alice"

        chain = _make_chain(
            iter_count_for=lambda i: 1,
            reporter_for=_reporter,
        )
        event = _make_comment_event(
            issue_key="PAY-9999",
            actor_account_id="alice",
        )

        chain.evaluate(event)

        assert captured == ["PAY-9999"]


# ---------------------------------------------------------------------------
# Stage ordering - composition properties
# ---------------------------------------------------------------------------


class TestMidChainStageOrdering:
    """The chain runs streamlit_bypass → replay_dedup → mention_filter.

    The composite tests pin the ordering invariants from the design's
    decision diagram. The unit-level alternatives above each pin the
    relative precedence of two adjacent stages; this class adds the
    transitive end-to-end checks so a mid-chain shuffle (eg. swapping
    replay_dedup and mention_filter) surfaces here.
    """

    def test_streamlit_bypass_dominates_replay_dedup_and_mention_filter(
        self,
    ) -> None:
        """``[bot:hear]`` survives even when both later stages would drop.

        With ``is_processed`` returning True (replay_dedup would drop)
        AND iter > 1 + actor ∉ mention_set (mention_filter would drop),
        the chain still passes the event through with the V12 reason.

        """

        chain = _make_chain(
            is_processed=lambda d: True,
            iter_count_for=lambda i: 5,
            mention_set_for=lambda i: frozenset(),
        )
        event = _make_comment_event(
            actor_account_id="random-third-party",
            body_text="[bot:hear] proxy reply",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS

    def test_replay_dedup_dominates_mention_filter(self) -> None:
        """A duplicate comment drops with the dedup reason, not Y6.

        With both stages firing (``is_processed`` True AND iter > 1 +
        unauthorised actor), the chain produces ``duplicate_event_dropped``.
        Operators want to know the event was a duplicate of a real
        prior delivery, not that the actor was unauthorised - the
        former is more actionable for diagnosing webhook retry storms.

        """

        chain = _make_chain(
            is_processed=lambda d: True,
            iter_count_for=lambda i: 5,
            mention_set_for=lambda i: frozenset(),
        )
        event = _make_comment_event(actor_account_id="random-third-party")

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_DUPLICATE_EVENT_DROPPED

    def test_mention_filter_runs_only_after_dedup_clears(self) -> None:
        """mention_filter sees the event only when replay_dedup falls through.

        We assert this by counting calls into ``iter_count_for`` -
        only mention_filter consults it. With ``is_processed`` True,
        the count must stay zero; flipping ``is_processed`` to False
        bumps the count to exactly 1.

        """

        seen_processed: bool = True
        iter_calls = 0

        def _iter_count(_issue: str) -> int:
            nonlocal iter_calls
            iter_calls += 1
            return 5

        chain = _make_chain(
            is_processed=lambda d: seen_processed,
            iter_count_for=_iter_count,
            mention_set_for=lambda i: frozenset(),
        )
        event = _make_comment_event(actor_account_id="random")

        # First call: dedup drops, mention_filter never runs.
        chain.evaluate(event)
        assert iter_calls == 0

        # Second call with ``is_processed=False``: dedup falls through,
        # mention_filter consults ``iter_count_for`` exactly once.
        seen_processed = False
        chain.evaluate(event)
        assert iter_calls == 1

    def test_first_iter_exception_runs_within_mention_filter_stage(
        self,
    ) -> None:
        """Z6 lives inside the mention_filter stage, so it runs after dedup.

        A duplicate event MUST still drop on dedup even when Z6
        would otherwise apply (iter == 1 + actor == reporter).
        Z6's purpose is to bypass Y6, not to bypass the dedup table -
        a duplicate delivery is a wasteful workflow signal regardless
        of who authored the original.

        """

        chain = _make_chain(
            is_processed=lambda d: True,
            iter_count_for=lambda i: 1,
            reporter_for=lambda i: "alice",
        )
        event = _make_comment_event(
            actor_account_id="alice",  # would normally fire Z6
            body_text="my first comment",
        )

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_DUPLICATE_EVENT_DROPPED

    def test_default_pass_reason_when_no_mid_chain_stage_fires(self) -> None:
        """A clean event reaches ``filter_chain_pass`` at the chain tail.

        Pins the canonical "everything's fine, dispatch this event"
        verdict so the FastAPI router can map it to
        HTTP 202 unambiguously.

        """

        chain = _make_chain(
            is_processed=lambda d: False,
            iter_count_for=lambda i: 2,
            mention_set_for=lambda i: frozenset({"alice"}),
        )
        event = _make_comment_event(
            actor_account_id="alice",
            body_text="hi alice mentioned me",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS
