"""Unit tests for stages of :class:`WebhookFilterChain`.

This test module pins the behaviour of the three filter-chain stages
used by the webhook workflow:

* ``_stage_verify_hmac`` raises
  :class:`automation_service.webhook_filters.WebhookHmacInvalidError`
  on miss.
* ``_stage_resolve_dept`` raises
  :class:`automation_service.webhook_filters.WebhookDeptUnresolvedError`
  on miss.
* ``_stage_loop_guard`` returns either
  ``loop_guard_dropped`` (actor-id match against the bot registry
  union) or ``loop_guard_regex_dropped`` (actor-id missing + body
  text matches ``BOT_PREFIX_REGEX``).

The tests instantiate the chain directly (no FastAPI / no HTTP) and
substitute pure callbacks for every collaborator. This keeps the test
matrix focused on the **decision logic** of each stage; the HTTP-side
mapping (401 / 400 / 200) is owned by the router unit tests in
``tests/unit/test_app.py``. The composite "all
stages chained together" coverage lives in the property suite at
``platform/tests/property/test_webhook_predicates.py``.

Test coverage maps each stage to positive, negative, callback, and
ordering behavior.
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
# legacy ``from src.config import Settings`` re-export. The first
# entry resolves the ``automation_service`` package; the second
# resolves the legacy ``src`` re-export. Mirrors the bootstrap used
# by ``tests/unit/test_jira_field_resolver.py`` and
# ``test_credentials.py``.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

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


# ---------------------------------------------------------------------------
# Helpers — minimal callback set for chains under test
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

    Every parameter has a sensible "no-op" default so each test can
    override only the callback it cares about. The defaults model a
    healthy webhook delivery: HMAC verifies, dept resolves to
    ``"payments"``, no bots registered, no replay history, no mentions,
    iter 0, reporter ``"reporter-1"``.
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
    """Build a :class:`WebhookEvent` with safe Jira defaults.

    The defaults match a typical ``jira:issue_created`` payload — the
    callers override individual fields for the negative cases (missing
    actor, bot actor, ``[bot:`` body text, etc.).
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
# verify_hmac stage
# ---------------------------------------------------------------------------


class TestVerifyHmacStage:
    """``_stage_verify_hmac`` raises on miss, falls through on hit.

    The runtime ``verify_hmac`` callback delegates to the foundation
    helper :func:`vault_client.verify_webhook_hmac` (with its 1h
    rotation overlap window). The chain only sees the boolean result;
    these tests pin the chain-side translation of that result into
    either pass-through or :class:`WebhookHmacInvalidError`.
    """

    def test_valid_hmac_passes_through(self) -> None:
        """A truthy ``verify_hmac`` callback lets the chain proceed.

        """

        chain = _make_chain(verify_hmac=lambda ev: True)
        event = _make_event()

        decision = chain.evaluate(event)

        # No drop fired — the chain reaches the default ``filter_chain_pass``
        # because every other stage's callback also returns the no-op
        # default. Pinning the action is sufficient; the precise reason
        # is a stability detail covered by the dedicated stage tests.
        assert decision.action == "pass"

    def test_invalid_hmac_raises(self) -> None:
        """A falsy ``verify_hmac`` callback raises with the canonical reason.

        """

        chain = _make_chain(verify_hmac=lambda ev: False)
        event = _make_event()

        with pytest.raises(WebhookHmacInvalidError) as excinfo:
            chain.evaluate(event)

        # The exception's class-level ``reason`` attribute is what the
        # router writes into the audit row, so we pin the literal here
        # to catch silent renames.
        assert excinfo.value.reason == REASON_WEBHOOK_HMAC_INVALID
        assert REASON_WEBHOOK_HMAC_INVALID == "webhook_hmac_invalid"

    def test_verify_hmac_callback_receives_full_event(self) -> None:
        """The chain forwards the entire event to ``verify_hmac``.

        The verifier needs ``raw_payload`` and the dialect-specific
        signature header to recompute the digest; passing the full
        :class:`WebhookEvent` (rather than just the body bytes) keeps
        the callback's implementation choices open. This test pins
        the contract.

        """

        captured: list[WebhookEvent] = []

        def _capture(ev: WebhookEvent) -> bool:
            captured.append(ev)
            return True

        chain = _make_chain(verify_hmac=_capture)
        event = _make_event(delivery_id="captured-delivery")

        chain.evaluate(event)

        assert len(captured) == 1
        assert captured[0] is event  # identity, not equality

    def test_hmac_failure_blocks_dept_resolution(self) -> None:
        """HMAC failure short-circuits before ``resolve_dept`` is consulted.

        The router relies on this ordering: a forged signature must
        not leak the project_key → dept_id mapping (or the absence
        thereof) via differential responses.

        """

        dept_calls = 0

        def _dept(ev: WebhookEvent) -> str | None:
            nonlocal dept_calls
            dept_calls += 1
            return "payments"

        chain = _make_chain(
            verify_hmac=lambda ev: False,
            resolve_dept=_dept,
        )
        event = _make_event()

        with pytest.raises(WebhookHmacInvalidError):
            chain.evaluate(event)

        assert dept_calls == 0


# ---------------------------------------------------------------------------
# resolve_dept stage
# ---------------------------------------------------------------------------


class TestResolveDeptStage:
    """``_stage_resolve_dept`` raises when no department owns the event."""

    def test_dept_resolved_passes_through(self) -> None:
        """A non-None dept_id lets the chain proceed.

        """

        chain = _make_chain(resolve_dept=lambda ev: "payments")
        event = _make_event()

        decision = chain.evaluate(event)

        assert decision.action == "pass"

    def test_unresolved_dept_raises(self) -> None:
        """``resolve_dept`` returning ``None`` raises with the canonical reason.

        """

        chain = _make_chain(resolve_dept=lambda ev: None)
        event = _make_event()

        with pytest.raises(WebhookDeptUnresolvedError) as excinfo:
            chain.evaluate(event)

        assert excinfo.value.reason == REASON_WEBHOOK_DEPT_UNRESOLVED
        assert REASON_WEBHOOK_DEPT_UNRESOLVED == "webhook_dept_unresolved"

    def test_resolve_dept_works_for_bitbucket_repo_slug(self) -> None:
        """Bitbucket events are dispatched to the same callback shape.

        The chain does not care whether the dept is keyed by
        ``project_key`` or ``repo_slug`` — both dialects flow through
        the same ``resolve_dept`` callback, which inspects the event
        and returns the appropriate dept_id.

        """

        # Pretend the dept registry only knows about the bitbucket
        # repo_slug. The callback below hardcodes that lookup so we
        # can prove the chain forwards a Bitbucket-shaped event
        # unchanged.
        def _resolve(ev: WebhookEvent) -> str | None:
            return "payments" if ev.repo_slug == "ws/payment-callbacks" else None

        chain = _make_chain(resolve_dept=_resolve)
        event = _make_event(
            provider="bitbucket",
            event_type="pullrequest:created",
            project_key=None,
            repo_slug="ws/payment-callbacks",
            issue_key=None,
            pr_id=42,
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    def test_unresolved_dept_blocks_loop_guard(self) -> None:
        """Dept-unresolved short-circuits before the loop guard runs.

        A bot self-action against an unregistered project should
        surface as ``webhook_dept_unresolved`` (the actionable
        operator signal) rather than ``loop_guard_dropped`` — the
        latter would silently swallow the misconfiguration.

        """

        loop_calls = 0

        def _bots() -> frozenset[str]:
            nonlocal loop_calls
            loop_calls += 1
            return frozenset({"bot-account-1"})

        chain = _make_chain(
            resolve_dept=lambda ev: None,
            bot_account_ids=_bots,
        )
        event = _make_event(actor_account_id="bot-account-1")

        with pytest.raises(WebhookDeptUnresolvedError):
            chain.evaluate(event)

        assert loop_calls == 0


# ---------------------------------------------------------------------------
# loop_guard stage
# ---------------------------------------------------------------------------


class TestLoopGuardActorIdStage:
    """``_stage_loop_guard`` drops events whose actor is in the bot union."""

    def test_actor_matching_bot_drops_with_loop_guard_dropped(self) -> None:
        """Actor-id ∈ bot_account_ids() → drop with ``loop_guard_dropped``.

        """

        chain = _make_chain(
            bot_account_ids=lambda: frozenset({"bot-account-1", "bot-account-2"}),
        )
        event = _make_event(actor_account_id="bot-account-1")

        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_DROPPED
        assert REASON_LOOP_GUARD_DROPPED == "loop_guard_dropped"

    def test_actor_not_in_registry_passes_loop_guard(self) -> None:
        """A human actor is allowed through.

        """

        chain = _make_chain(
            bot_account_ids=lambda: frozenset({"bot-account-1"}),
        )
        event = _make_event(actor_account_id="human-user-99")

        decision = chain.evaluate(event)

        assert decision.action == "pass"

    def test_empty_bot_registry_never_triggers_loop_guard(self) -> None:
        """Boot-time edge case: empty registry passes every event through.

        A fresh install has no departments, hence no bot accounts. The
        loop guard must not silently drop every webhook in this state
        — this startup invariant carries through.

        """

        chain = _make_chain(bot_account_ids=lambda: frozenset())
        event = _make_event(actor_account_id="any-user")

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    def test_cross_dept_bot_still_triggers_loop_guard(self) -> None:
        """A bot from dept A still loops when commenting on dept B.

        The registry the chain consults is the **flat union** of every
        department's bots, so bot account IDs from any dept short-
        circuit the loop. This matches the design invariant that
        cross-dept bot activity cannot create a loop.

        """

        chain = _make_chain(
            # ``payments`` resolves regardless of project_key in this
            # test; the bot however belongs to ``research``.
            resolve_dept=lambda ev: "payments",
            bot_account_ids=lambda: frozenset(
                {"payments-bot", "research-bot", "platform-bot"}
            ),
        )
        # The actor is the research-bot but the dept resolves to
        # payments — cross-dept self-action.
        event = _make_event(actor_account_id="research-bot")

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_DROPPED

    def test_bot_registry_is_called_per_event(self) -> None:
        """The chain re-invokes ``bot_account_ids`` for every event.

        The contract documented on the constructor is that the
        callback is "lazily-evaluated" so registry refreshes (a new
        dept boots, a bot rotates) propagate without rebuilding the
        chain. This test pins that lazy semantics.

        """

        calls = 0

        def _bots() -> frozenset[str]:
            nonlocal calls
            calls += 1
            return frozenset({f"bot-after-{calls}-calls"})

        chain = _make_chain(bot_account_ids=_bots)

        chain.evaluate(_make_event(actor_account_id="x"))
        chain.evaluate(_make_event(actor_account_id="y"))
        chain.evaluate(_make_event(actor_account_id="z"))

        assert calls == 3


# ---------------------------------------------------------------------------
# loop_guard regex fallback
# ---------------------------------------------------------------------------


class TestLoopGuardRegexFallbackStage:
    """``[bot:`` regex fallback fires only when ``actor_account_id`` is None."""

    def test_actor_none_with_bot_prefix_drops(self) -> None:
        """No actor + body starts with ``[bot:`` → drop with regex reason.

        """

        chain = _make_chain()
        event = _make_event(
            actor_account_id=None,
            body_text="[bot: I just commented]",
        )

        decision = chain.evaluate(event)

        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_REGEX_DROPPED
        assert REASON_LOOP_GUARD_REGEX_DROPPED == "loop_guard_regex_dropped"

    @pytest.mark.parametrize(
        "leading_whitespace",
        ["", " ", "  ", "\t", " \t "],
    )
    def test_regex_allows_leading_whitespace(
        self, leading_whitespace: str
    ) -> None:
        """The ``^\\s*\\[bot:`` regex tolerates leading whitespace.

        Editors and clients sometimes pad comment bodies; the regex
        anchor allows zero or more whitespace characters before the
        ``[bot:`` token.

        """

        chain = _make_chain()
        body = f"{leading_whitespace}[bot: hello]"
        event = _make_event(actor_account_id=None, body_text=body)

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        assert decision.reason == REASON_LOOP_GUARD_REGEX_DROPPED

    def test_actor_none_without_bot_prefix_passes(self) -> None:
        """No actor + body without ``[bot:`` → pass through.

        """

        chain = _make_chain()
        event = _make_event(
            actor_account_id=None,
            body_text="this is a normal comment from a system event",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    def test_actor_none_with_none_body_passes(self) -> None:
        """No actor + no body text → pass through.

        Some Atlassian system events ship neither an actor nor a
        comment body (e.g. lifecycle hooks). The chain must let them
        through cleanly so subsequent stages can decide.

        """

        chain = _make_chain()
        event = _make_event(actor_account_id=None, body_text=None)

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    def test_human_actor_with_bot_quote_does_not_trigger_regex(self) -> None:
        """A human author quoting ``[bot:`` is **not** treated as bot output.

        The regex fallback is gated on ``actor_account_id is None``;
        a real human commenting "the bot wrote ``[bot: hi]`` earlier"
        must not be caught by the loop guard. This pins the gating
        condition that separates the actor-id check from the regex
        fallback.

        """

        chain = _make_chain(
            bot_account_ids=lambda: frozenset({"bot-account-1"}),
        )
        event = _make_event(
            actor_account_id="human-user",
            body_text="[bot: looks like the bot did something]",
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    @pytest.mark.parametrize(
        "non_matching_body",
        [
            "hello world",
            "[user: hi]",
            "  some [bot: not-at-start]",  # ``[bot:`` is not at line start
            "Bot replied with [bot:hear]",  # not at start of body
            "",
        ],
    )
    def test_regex_does_not_match_unrelated_bodies(
        self, non_matching_body: str
    ) -> None:
        """Bodies that do not lead with ``[bot:`` survive the regex.

        Pins the regex shape against false-positive shrinkage
        attempts: the pattern is anchored to the start of the string
        with optional whitespace, never anywhere else.

        """

        chain = _make_chain()
        event = _make_event(
            actor_account_id=None,
            body_text=non_matching_body if non_matching_body else None,
        )

        decision = chain.evaluate(event)
        assert decision.action == "pass"

    def test_bot_prefix_regex_constant_shape(self) -> None:
        """The exported :data:`BOT_PREFIX_REGEX` is the design's pattern.

        Pins the literal pattern so any future relaxation that breaks
        the contract surfaces as a test diff rather than a silent
        behaviour change.

        """

        # ``re.Pattern.pattern`` is the source string; this assertion
        # specifies ``^\s*\[bot:`` exactly.
        assert BOT_PREFIX_REGEX.pattern == r"^\s*\[bot:"
        # Sanity: the pattern matches and rejects the canonical examples.
        assert BOT_PREFIX_REGEX.search("[bot: yes]") is not None
        assert BOT_PREFIX_REGEX.search("  [bot: yes]") is not None
        assert BOT_PREFIX_REGEX.search("not [bot: no]") is None


# ---------------------------------------------------------------------------
# Stage ordering — composition properties
# ---------------------------------------------------------------------------


class TestEvaluateStageOrdering:
    """The chain runs verify → resolve → loop in that fixed order."""

    def test_hmac_runs_before_dept_resolve(self) -> None:
        """HMAC failure surfaces even when no dept is configured.

        """

        chain = _make_chain(
            verify_hmac=lambda ev: False,
            resolve_dept=lambda ev: None,  # would otherwise raise dept error
        )
        with pytest.raises(WebhookHmacInvalidError):
            chain.evaluate(_make_event())

    def test_dept_resolve_runs_before_loop_guard(self) -> None:
        """Dept failure surfaces even when the actor is a bot.

        """

        chain = _make_chain(
            resolve_dept=lambda ev: None,
            bot_account_ids=lambda: frozenset({"bot-1"}),
        )
        with pytest.raises(WebhookDeptUnresolvedError):
            chain.evaluate(_make_event(actor_account_id="bot-1"))

    def test_loop_guard_runs_before_regex_fallback_when_actor_present(
        self,
    ) -> None:
        """Actor-id check beats body-text scan when both could match.

        When the actor IS in the bot registry AND the body starts
        with ``[bot:``, only the actor-id reason should fire — the
        regex fallback exists specifically for the case where the
        actor is missing.

        """

        chain = _make_chain(
            bot_account_ids=lambda: frozenset({"bot-account-1"}),
        )
        event = _make_event(
            actor_account_id="bot-account-1",
            body_text="[bot: also matches the regex]",
        )

        decision = chain.evaluate(event)
        assert decision.action == "drop"
        # Specifically the actor-id reason, not the regex one.
        assert decision.reason == REASON_LOOP_GUARD_DROPPED

    def test_loop_guard_drop_short_circuits_remaining_stages(self) -> None:
        """A loop-guard drop never consults ``is_processed`` etc.

        The chain must not waste a Postgres round-trip for an event
        we are about to drop with audit ``loop_guard_dropped``.

        """

        is_processed_calls = 0

        def _is_processed(d: str) -> bool:
            nonlocal is_processed_calls
            is_processed_calls += 1
            return False

        chain = _make_chain(
            bot_account_ids=lambda: frozenset({"bot-account-1"}),
            is_processed=_is_processed,
        )
        event = _make_event(actor_account_id="bot-account-1")

        chain.evaluate(event)

        assert is_processed_calls == 0
