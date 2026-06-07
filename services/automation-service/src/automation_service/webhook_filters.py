"""Webhook filter chain.

This module hosts the **deterministic decision pipeline** for the
webhook gateway. The filter stages cover HMAC verification, department
resolution, loop guarding, replay deduplication, mention filtering,
the first-iteration exception, the ``[bot:hear]`` bypass, and
comment-burst debounce.

What this module owns
---------------------

1. :class:`WebhookEvent` - a frozen dataclass that **normalises the
   Jira and Bitbucket webhook dialects to a single shape**. Atlassian
   Jira encodes the event type in the JSON body's ``webhookEvent``
   field, while Bitbucket carries it in the ``X-Event-Key`` HTTP
   header. The chain operates on a normalised, dialect-agnostic value
   object so the rest of the pipeline can stay protocol-pure.

2. :class:`FilterDecision` - a frozen dataclass that captures the
   chain's verdict (``"drop"`` or ``"pass"``), an audit-friendly
   ``reason`` string (e.g. ``"loop_guard_dropped"`` or
   ``"comment_ignored_unauthorized_actor"``), and an optional tuple of
   delivery_ids that were merged together by the burst-debounce stage
   (populated only when ``coalesced_with`` is non-empty).

3. :class:`WebhookFilterChain` - the orchestration class. Its
   constructor accepts callbacks that compose into the actual filter
   logic. The :meth:`evaluate` method runs the stages in this order:

   ```
   verify_hmac    resolve_dept    loop_guard
         streamlit_bypass     # [bot:hear] takes precedence
         replay_dedup
         mention_filter (with first_iter_exception merged in)
         burst_debounce
         pass
   ```

Determinism contract
--------------------

Every callback the chain accepts must be a **pure function of its
input**. The chain itself performs no I/O - it only routes the event
through the callbacks supplied at construction time. This mirrors the
filter-chain contract
and keeps the chain testable with hypothesis-driven event sequences.

Behaviour covered
-----------------

* A single normalised entry point handles ``POST /webhooks/jira`` and
  ``POST /webhooks/bitbucket`` through :class:`WebhookEvent`.
* Jira event types are captured by the ``event_type`` field; the
  ``mention_filter`` stage only fires for ``issue_commented``.
* Bitbucket event types land in the same ``event_type`` field, and
  ``pullrequest:fulfilled`` is recognised and dropped by the loop
  guard rather than treated as an unknown event.
* HMAC verification is delegated to the ``verify_hmac`` callback whose
  runtime implementation reads ``vault:webhooks/<provider>/<dept_id>``.
* Department resolution is delegated to ``resolve_dept``; unresolved
  departments surface through a clean HTTP 400 path.
* ``mention_filter`` drops ``issue_commented`` events whose actor is
  not in the bot-mentioned set and whose ``iter_count > 1``; audit
  reason is ``comment_ignored_unauthorized_actor``.
* ``first_iter_exception`` bypasses the mention filter when
  ``iter_count == 1`` AND the actor matches the issue reporter; the
  chain returns ``pass`` with reason
  ``mention_filter_first_iter_exception``.
* ``streamlit_bypass`` detects the ``[bot:hear]`` etiquette tag in the
  comment body and short-circuits to ``pass`` with audit reason
  ``streamlit_inline_reply_with_bypass``. This bypass takes precedence
  over both ``replay_dedup`` and ``mention_filter``.

Note: the behavioural tests live in ``test_webhook_predicates.py``.
The unit-level coverage for the mid-chain stages lives in
``tests/unit/test_webhook_filters_mid_chain.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Final,
    Literal,
    Mapping,
)

__all__ = [
    "WebhookEvent",
    "FilterDecision",
    "WebhookFilterChain",
    "WebhookHmacInvalidError",
    "WebhookDeptUnresolvedError",
    "JIRA_EVENT_TYPES",
    "BITBUCKET_EVENT_TYPES",
    "JIRA_COMMENT_EVENT_TYPE",
    "STREAMLIT_BYPASS_TAG",
    "BOT_PREFIX_REGEX",
    "REASON_WEBHOOK_HMAC_INVALID",
    "REASON_WEBHOOK_DEPT_UNRESOLVED",
    "REASON_LOOP_GUARD_DROPPED",
    "REASON_LOOP_GUARD_REGEX_DROPPED",
    "REASON_DUPLICATE_EVENT_DROPPED",
    "REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR",
    "REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION",
    "REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS",
    "REASON_BURST_COALESCED",
    "REASON_FILTER_CHAIN_PASS",
    "REASON_FILTER_CHAIN_SKELETON",
    "BurstRegister",
    "BurstRegisterResult",
    "normalize_jira_event",
    "normalize_bitbucket_event",
]


# ---------------------------------------------------------------------------
# Audit reason constants
# ---------------------------------------------------------------------------
#
# These short, dot-free identifiers are the audit-friendly labels the
# router writes to ``audit_events.action`` when a filter stage produces
# a verdict. They are exported so call-sites (the FastAPI router in
# the router and the unit / property tests) can match against the
# constant rather than retyping the literal - typos in audit labels
# are otherwise silent.

#: Jira webhook event type that the mention filter / first-iter
#: exception are scoped to. Kept as a separate constant so neither the
#: chain nor the tests need to retype the literal.
JIRA_COMMENT_EVENT_TYPE: Final[str] = "jira:issue_commented"

#: ``[bot:hear]`` etiquette tag. Streamlit's inline-reply UI
#: prepends or embeds this tag in the comment body so the chain knows
#: the comment originated from the bot's own UI and should bypass both
#: replay-dedup and the mention filter. Compared case-insensitively
#: against the entire ``body_text`` so editors that auto-capitalise or
#: pad the tag (``[Bot:hear]`` / `` [bot:hear] ``) still take effect.
STREAMLIT_BYPASS_TAG: Final[str] = "[bot:hear]"

#: Audit reason emitted when ``replay_dedup`` drops an event whose
#: ``delivery_id`` is already recorded in the ``processed_events``
#: table.
REASON_DUPLICATE_EVENT_DROPPED: Final[str] = "duplicate_event_dropped"

#: Audit reason emitted when ``mention_filter`` drops a comment whose
#: actor is not in the bot-mentioned set for the issue (iter > 1, no
#: bypass applies.
REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR: Final[str] = (
    "comment_ignored_unauthorized_actor"
)

#: Audit reason emitted when the first-iter exception bypasses
#: ``mention_filter``. The chain passes the event through and the
#: router records this label so operators can trace why a non-mention
#: comment was honoured.
REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION: Final[str] = (
    "mention_filter_first_iter_exception"
)

#: Audit reason emitted when ``streamlit_bypass`` detects the
#: ``[bot:hear]`` tag and short-circuits the chain to ``pass``.
REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS: Final[str] = (
    "streamlit_inline_reply_with_bypass"
)

#: Audit reason emitted when the chain reaches the end without a drop
#: or bypass - the canonical "filter chain accepted the event" label.
REASON_FILTER_CHAIN_PASS: Final[str] = "filter_chain_pass"

#: Compiled regex matching :data:`STREAMLIT_BYPASS_TAG` anywhere in
#: ``body_text``. ``re.IGNORECASE`` is used so casing variants of the
#: tag still trigger the bypass; ``re.escape`` defends against any
#: future change that adds regex meta-characters to the tag. The
#: pattern is module-private - callers should use
#: :meth:`WebhookFilterChain._has_streamlit_bypass_tag` (or the
#: equivalent simple ``in`` check on the lower-cased body) so the tag
#: detection stays consistent across the chain and the tests.
_STREAMLIT_BYPASS_TAG_RE: Final[re.Pattern[str]] = re.compile(
    re.escape(STREAMLIT_BYPASS_TAG), re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Event-type constants
# ---------------------------------------------------------------------------
#
# Jira webhook payloads carry the event type under the top-level
# ``webhookEvent`` key (e.g. ``"jira:issue_created"``). Bitbucket Cloud
# uses the ``X-Event-Key`` header (e.g. ``"pullrequest:created"``). We
# expose both sets as immutable mappings so other modules - notably the
# routing stage can validate against the same canonical
# table without needing to import the FastAPI router.

#: Jira webhook event types supported by the automation gateway.
#: Order is irrelevant; the value is a stable display label used in
#: audit log payloads.
JIRA_EVENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "jira:issue_created": "issue_created",
        "jira:issue_assigned": "issue_assigned",
        "jira:issue_updated": "issue_updated",
        "jira:issue_commented": "issue_commented",
    }
)

#: Bitbucket webhook event types supported by the automation gateway.
#: ``pullrequest:fulfilled`` is included because it must be **explicitly
#: dropped** by the loop-guard stage - it's a recognised
#: event whose action is "drop", not an unknown event whose action is
#: "ignore".
BITBUCKET_EVENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "pullrequest:created": "pr_created",
        "pullrequest:commented": "pr_commented",
        "pullrequest:updated": "pr_updated",
        "pullrequest:fulfilled": "pr_fulfilled",
    }
)


# ---------------------------------------------------------------------------
# Stage-specific constants (HMAC, dept resolve, loop guard)
# ---------------------------------------------------------------------------
#
# These constants are exposed at module level so the FastAPI router
# and the audit-log writer can both reference the same
# canonical reason strings without duplicating literals across modules.
# ``JIRA_COMMENT_EVENT_TYPE``, ``STREAMLIT_BYPASS_TAG``, and the four
# mid-chain ``REASON_*`` constants
# (``REASON_DUPLICATE_EVENT_DROPPED``,
# ``REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR``,
# ``REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION``,
# ``REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS``) plus
# ``REASON_FILTER_CHAIN_PASS`` and ``REASON_BURST_COALESCED`` live with
# the declarations above; this block adds the HMAC/dept/loop-guard
# specific labels and fallback regex on top.

#: Loop-guard fallback regex. When
#: ``actor_account_id`` is missing from the payload - typical for
#: Jira's older comment delivery shapes that omit ``user`` - the chain
#: scans ``body_text`` for the ``[bot:`` prefix at the start of the
#: line (with optional leading whitespace). A match means the comment
#: was *itself* authored by a bot under a previous account scheme; the
#: event is dropped to break the loop.
#:
#: The pattern is compiled once at import time so the per-event hot
#: path (one regex search per Jira comment) stays cheap.
BOT_PREFIX_REGEX: Final[re.Pattern[str]] = re.compile(r"^\s*\[bot:")

#: Audit reason for HMAC verification failures.
#: Surfaced through :class:`WebhookHmacInvalidError`; mapped to HTTP
#: 401 by the FastAPI router.
REASON_WEBHOOK_HMAC_INVALID: Final[str] = "webhook_hmac_invalid"

#: Audit reason for unresolved-dept rejections.
#: Surfaced through :class:`WebhookDeptUnresolvedError`; mapped to
#: HTTP 400 by the FastAPI router.
REASON_WEBHOOK_DEPT_UNRESOLVED: Final[str] = "webhook_dept_unresolved"

#: Audit reason for actor-id loop-guard hits.
#: The chain returns ``FilterDecision(action="drop")`` with this
#: reason; the router maps it to HTTP 200.
REASON_LOOP_GUARD_DROPPED: Final[str] = "loop_guard_dropped"

#: Audit reason for body-text loop-guard hits. Distinct from
#: :data:`REASON_LOOP_GUARD_DROPPED` so operators
#: can diagnose the *source* of every loop drop - actor-id matches
#: indicate registry hits, regex matches indicate a missing or
#: malformed actor field.
REASON_LOOP_GUARD_REGEX_DROPPED: Final[str] = "loop_guard_regex_dropped"

#: Audit reason emitted when the burst-debounce stage
#: drops a delivery because an open 3-second window already has the
#: same ``issue_key`` buffered. The dropped delivery_id is appended
#: to the buffer's ``coalesced_with`` list and surfaces via
#: :class:`FilterDecision.coalesced_with`. Defined here
#: because the constant is referenced by ``_stage_burst_debounce``
#: which already exists in this file ahead of burst-window wiring.
REASON_BURST_COALESCED: Final[str] = "burst_coalesced"

#: Skeleton reason still emitted by the chain when no stage logic is
#: wired. Preserved for backwards compatibility with the original
#: signature contract; no event
#: surfaces this reason - but the constant remains so unit tests that
#: pin the skeleton-era behaviour can still import it.
REASON_FILTER_CHAIN_SKELETON: Final[str] = "filter_chain_skeleton"


# ---------------------------------------------------------------------------
# Stage exceptions - HTTP-level failures (HMAC invalid, dept unresolved)
# ---------------------------------------------------------------------------
#
# The chain treats HMAC failures (HTTP 401) and unresolved-dept
# rejections (HTTP 400) as *errors* rather than ``"drop"`` decisions
# because they carry an HTTP semantic that the rest of the chain's
# verdicts (``"drop" | "pass"``) cannot express. The router
# catches these specific exception types and translates them into the
# appropriate response + audit pair; every other ``Exception`` is
# treated as an unexpected 500.


class WebhookHmacInvalidError(Exception):
    """Raised by :meth:`WebhookFilterChain.evaluate` when HMAC fails.

    Carries the audit reason :data:`REASON_WEBHOOK_HMAC_INVALID` so the
    FastAPI router can write a uniform audit row regardless
    of which webhook endpoint surfaced the failure. The router maps
    this exception to HTTP 401.

    The error intentionally **does not** capture the failing signature
    or the secret being matched against - those are sensitive enough
    that a stack trace from this exception must remain inert.
    """

    #: Canonical audit reason string for this error. Class-level so
    #: the router can branch on the exception type alone without
    #: instantiating it.
    reason: Final[str] = REASON_WEBHOOK_HMAC_INVALID

    def __init__(self, message: str = "webhook HMAC signature invalid") -> None:
        super().__init__(message)


class WebhookDeptUnresolvedError(Exception):
    """Raised by :meth:`WebhookFilterChain.evaluate` for unresolved dept.

    Carries the audit reason :data:`REASON_WEBHOOK_DEPT_UNRESOLVED`.
    The router maps this exception to HTTP 400. The Jira and Bitbucket
    dialects both surface the same exception so the router does not
    need to distinguish between ``project_key`` and ``repo_slug``
    misconfiguration at the HTTP layer.
    """

    reason: Final[str] = REASON_WEBHOOK_DEPT_UNRESOLVED

    def __init__(
        self,
        message: str = "webhook event could not be mapped to a department",
    ) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# WebhookEvent - normalised value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """Normalised webhook event shared by Jira and Bitbucket dialects.

    The value object is deliberately *flat* - every field is either a
    primitive or an immutable container - so the chain can hash, copy,
    and serialise events without surprises. Optional fields default to
    ``None`` so a single :class:`WebhookEvent` shape covers both
    dialects without conditional ``hasattr`` checks downstream.

    Field semantics
    ---------------

    * :attr:`provider` - ``"jira"`` or ``"bitbucket"``. Disambiguates
      the dialect-specific HMAC header (``X-Atlassian-Webhook-Signature``
      vs. ``X-Hub-Signature``) and the per-provider Vault secret path
      (``vault:webhooks/jira/<dept_id>`` vs.
      ``vault:webhooks/bitbucket/<dept_id>``).
    * :attr:`event_type` - the **raw** Jira ``webhookEvent`` value or
      Bitbucket ``X-Event-Key`` header value (e.g.
      ``"jira:issue_commented"``, ``"pullrequest:created"``). The
      normalised display labels live in :data:`JIRA_EVENT_TYPES` /
      :data:`BITBUCKET_EVENT_TYPES`.
    * :attr:`delivery_id` - the platform's idempotency key. For Jira
      this is the ``X-Request-Id`` (or ``X-Atlassian-Webhook-Identifier``
      depending on tenancy); for Bitbucket it's the ``X-Request-UUID``.
      The replay-dedup stage hashes this against the
      ``processed_events`` table.
    * :attr:`actor_account_id` - the ``accountId`` of the user who
      triggered the event. May be ``None`` for system events; the
      loop-guard fallback then scans ``body_text`` for the
      ``[bot:`` prefix.
    * :attr:`body_text` - comment body or PR title. Used by the
      ``[bot:`` regex fallback and by the ``[bot:hear]`` bypass.
    * :attr:`project_key` - Jira project key (e.g. ``"PAY"``); ``None``
      for Bitbucket events. Used by ``resolve_dept`` to look up the
      department.
    * :attr:`repo_slug` - Bitbucket repo slug (e.g.
      ``"payment-callbacks"``); ``None`` for Jira events. Also used by
      ``resolve_dept`` for Bitbucket-side resolution.
    * :attr:`issue_key` - full Jira issue key (e.g. ``"PAY-4211"``);
      drives ``mention_set_for`` / ``iter_count_for`` /
      ``reporter_for`` callbacks.
    * :attr:`pr_id` - Bitbucket PR id; mirrors :attr:`issue_key` for PR
      events.
    * :attr:`raw_payload` - the original parsed JSON body. Kept around
      so the HMAC verifier can recompute the signature against the
      exact bytes if it needs to.
    """

    provider: Literal["jira", "bitbucket"]
    event_type: str
    delivery_id: str
    actor_account_id: str | None = None
    body_text: str | None = None
    project_key: str | None = None
    repo_slug: str | None = None
    issue_key: str | None = None
    pr_id: int | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# FilterDecision - chain verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FilterDecision:
    """The chain's verdict for a single :class:`WebhookEvent`.

    The decision is **structural** - it tells the FastAPI router
    what HTTP status to return and what audit action to log, but
    it never carries an HTTP status itself. Mapping ``action`` /
    ``reason`` pairs to HTTP responses is the router's job.

    Fields
    ------

    * :attr:`action` - ``"drop"`` means the event is acknowledged but
      not propagated (returns ``200 OK`` from the router). ``"pass"``
      means the event proceeds to ``signalWithStart`` (returns
      ``202 Accepted``). The HMAC-failure and dept-unresolved cases
      surface as exceptions raised by the corresponding stages, not
      as ``"drop"`` decisions, because they correspond to ``401`` /
      ``400`` HTTP responses respectively.
    * :attr:`reason` - a short audit-friendly identifier. Known
      values: ``"loop_guard_dropped"``,
      ``"comment_ignored_unauthorized_actor"``,
      ``"streamlit_inline_reply_with_bypass"``,
      ``"mention_filter_first_iter_exception"``,
      ``"duplicate_event_dropped"``,
      ``"filter_chain_pass"``, ``"filter_chain_skeleton"``.
    * :attr:`coalesced_with` - delivery ids merged into this decision
      by the 3-second burst-debounce window. Empty for any event that
      isn't the *terminal* event of a debounced burst. The router
      writes these into the audit payload so operators can trace which
      duplicate deliveries were collapsed.
    """

    action: Literal["drop", "pass"]
    reason: str
    coalesced_with: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Callback type aliases
# ---------------------------------------------------------------------------
#
# We model every collaborator as a plain ``Callable`` rather than a
# ``Protocol`` so the chain stays trivially mockable in unit tests
# without an explicit class. The runtime
# implementations supplied by ``automation_service/app.py`` will be
# small lambdas / async wrappers around the existing infrastructure
# clients (vault, postgres, dept registry).

#: HMAC verifier: takes the event and returns ``True`` if the signature
#: matches the per-dept secret (with the foundation 1h overlap window).
VerifyHmac = Callable[[WebhookEvent], bool]

#: Department resolver: ``project_key`` (Jira) or ``repo_slug``
#: (Bitbucket)  ``dept_id`` or ``None`` if no department owns it.
ResolveDept = Callable[[WebhookEvent], str | None]

#: Bot account-id snapshot. The chain calls this lazily so the registry
#: can be refreshed without restarting the chain.
BotAccountIds = Callable[[], frozenset[str]]

#: Replay dedup probe: ``delivery_id``  ``True`` if the platform has
#: already processed this delivery.
IsProcessed = Callable[[str], bool]

#: Per-issue mention set: ``issue_key``  set of bot-mentioned account
#: ids. Used by the mention-filter stage.
MentionSetFor = Callable[[str], frozenset[str]]

#: Per-issue iteration counter: ``issue_key``  current iter_count.
IterCountFor = Callable[[str], int]

#: Per-issue reporter resolver: ``issue_key``  reporter account id.
#: Used by the first-iter exception.
ReporterFor = Callable[[str], str]


# ---------------------------------------------------------------------------
# Burst-debounce callback
# ---------------------------------------------------------------------------
#
# The burst-debounce stage is the only filter stage that owns
# *cross-event* state - it tracks an open 3-second window per
# ``issue_key`` and merges deliveries that arrive inside the window.
# That state lives in :class:`automation_service.burst_window.BurstWindow`,
# not inside the chain itself, so the chain stays a pure decision
# pipeline. The chain consumes a tiny adapter callback that wraps a
# :meth:`BurstWindow.register` invocation.
#
# Modelling this as a callback (rather than a hard dependency on the
# concrete ``BurstWindow`` class) keeps the chain testable: the
# property tests in ``tests/property/test_burst_debounce.py`` inject a
# ``BurstWindow`` instance directly, while unit tests for unrelated
# stages can pass a no-op stub.


@dataclass(frozen=True, slots=True)
class BurstRegisterResult:
    """Return shape for the :data:`BurstRegister` callback.

    Modelled as a frozen dataclass so the chain can pattern-match the
    decision without importing the
    :class:`~automation_service.burst_window.BurstWindow`-specific
    ``Literal`` type alias. The two fields are:

    * :attr:`decision` - ``"coalesce_emit"`` means the event opened a
      fresh window and the chain should pass it through (the caller
      will eventually flush the window and dispatch the *terminal*
      payload). ``"coalesce_dropped"`` means the event landed inside
      an open window and the chain returns
      ``FilterDecision(action="drop", reason="burst_coalesced",
      coalesced_with=...)``.
    * :attr:`coalesced_with` - delivery_ids accumulated in the open
      window, including the dropped delivery itself. The
      tuple is empty for ``"coalesce_emit"`` decisions because the
      window has only just opened with the current delivery as the
      anchor.
    """

    decision: Literal["coalesce_dropped", "coalesce_emit"]
    coalesced_with: tuple[str, ...] = ()


#: Burst-debounce callback signature.
#:
#: The chain calls the registered function once per evaluable
#: comment-burst event with the event itself as the sole argument. The
#: callback is responsible for translating the event into a
#: :meth:`BurstWindow.register` call (typically by extracting
#: ``issue_key``, ``delivery_id``, and ``raw_payload``, and by reading
#: ``time.monotonic()`` for the ``now`` argument). Returning ``None``
#: signals "this event is out of scope for the burst stage" and the
#: chain falls through to the default ``filter_chain_pass`` verdict.
#: Returning a :class:`BurstRegisterResult` triggers the chain's
#: drop / pass routing per the decision rules in :meth:`_stage_burst_debounce`.
BurstRegister = Callable[[WebhookEvent], BurstRegisterResult | None]


# ---------------------------------------------------------------------------
# Default burst window
# ---------------------------------------------------------------------------

#: Comment burst debounce window. The actual coalescing logic lives in
#: this constant is the canonical default that the chain
#: passes through to that stage.
_DEFAULT_BURST_WINDOW: Final[timedelta] = timedelta(seconds=3)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _coerce_str(value: Any) -> str | None:
    """Return ``value`` as ``str`` if it's a non-empty string, else ``None``.

    Webhook payloads are untrusted JSON: any field may be missing,
    null, or have an unexpected type. The normaliser uses this helper
    to defensively coerce optional fields without raising on dialect
    quirks.
    """

    if isinstance(value, str) and value:
        return value
    return None


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` as ``int`` if it's a non-bool int, else ``None``."""

    # ``bool`` is a subclass of ``int`` in Python; reject it explicitly
    # so a ``True`` from a malformed payload doesn't masquerade as a
    # PR id of ``1``.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def normalize_jira_event(
    *,
    raw_payload: Mapping[str, Any],
    delivery_id: str,
    event_type: str | None = None,
) -> WebhookEvent:
    """Normalise a Jira webhook payload into :class:`WebhookEvent`.

    Parameters
    ----------
    raw_payload:
        Parsed JSON body as delivered by Jira. The function reads
        ``webhookEvent`` (event type), ``user.accountId`` /
        ``comment.author.accountId`` (actor), ``comment.body`` (body
        text), ``issue.key``, and ``issue.fields.project.key``.
    delivery_id:
        Idempotency key, supplied by the FastAPI router from the
        ``X-Atlassian-Webhook-Identifier`` (or
        ``X-Request-Id``) header.
    event_type:
        Override for the event type, used when the FastAPI URL stem
        already pinpoints the event (e.g. ``/webhooks/jira/issue_created``)
        and the body's ``webhookEvent`` is missing or untrusted. When
        ``None``, falls back to ``raw_payload["webhookEvent"]``.

    Returns
    -------
    WebhookEvent
        A frozen value object whose :attr:`provider` is ``"jira"``.
    """

    resolved_event_type = (
        event_type or _coerce_str(raw_payload.get("webhookEvent")) or ""
    )

    actor_account_id: str | None = None
    user = raw_payload.get("user")
    if isinstance(user, Mapping):
        actor_account_id = _coerce_str(user.get("accountId"))
    if actor_account_id is None:
        comment = raw_payload.get("comment")
        if isinstance(comment, Mapping):
            author = comment.get("author")
            if isinstance(author, Mapping):
                actor_account_id = _coerce_str(author.get("accountId"))

    body_text: str | None = None
    comment = raw_payload.get("comment")
    if isinstance(comment, Mapping):
        body_text = _coerce_str(comment.get("body"))

    issue_key: str | None = None
    project_key: str | None = None
    issue = raw_payload.get("issue")
    if isinstance(issue, Mapping):
        issue_key = _coerce_str(issue.get("key"))
        fields = issue.get("fields")
        if isinstance(fields, Mapping):
            project = fields.get("project")
            if isinstance(project, Mapping):
                project_key = _coerce_str(project.get("key"))
    # Some payload shapes ship ``project`` at the top level; accept that too.
    if project_key is None:
        project = raw_payload.get("project")
        if isinstance(project, Mapping):
            project_key = _coerce_str(project.get("key"))

    return WebhookEvent(
        provider="jira",
        event_type=resolved_event_type,
        delivery_id=delivery_id,
        actor_account_id=actor_account_id,
        body_text=body_text,
        project_key=project_key,
        repo_slug=None,
        issue_key=issue_key,
        pr_id=None,
        raw_payload=raw_payload,
    )


def normalize_bitbucket_event(
    *,
    raw_payload: Mapping[str, Any],
    delivery_id: str,
    event_type: str,
) -> WebhookEvent:
    """Normalise a Bitbucket webhook payload into :class:`WebhookEvent`.

    Parameters
    ----------
    raw_payload:
        Parsed JSON body as delivered by Bitbucket Cloud (or DC). The
        function reads ``actor.account_id`` /
        ``actor.uuid`` (actor), ``pullrequest.title`` /
        ``comment.content.raw`` (body text),
        ``repository.full_name`` / ``repository.slug`` (repo), and
        ``pullrequest.id`` (PR id).
    delivery_id:
        Idempotency key, supplied by the FastAPI router from the
        ``X-Request-UUID`` header.
    event_type:
        The Bitbucket ``X-Event-Key`` header value (e.g.
        ``"pullrequest:created"``). Required because Bitbucket - unlike
        Jira - does **not** put the event type in the JSON body.

    Returns
    -------
    WebhookEvent
        A frozen value object whose :attr:`provider` is ``"bitbucket"``.
    """

    actor_account_id: str | None = None
    actor = raw_payload.get("actor")
    if isinstance(actor, Mapping):
        # Bitbucket Cloud uses ``account_id``; older DC payloads ship
        # ``uuid``. We accept either with ``account_id`` taking priority
        # because the loop-guard registry is keyed by the Cloud account id.
        actor_account_id = _coerce_str(actor.get("account_id"))
        if actor_account_id is None:
            actor_account_id = _coerce_str(actor.get("uuid"))

    body_text: str | None = None
    comment = raw_payload.get("comment")
    if isinstance(comment, Mapping):
        content = comment.get("content")
        if isinstance(content, Mapping):
            body_text = _coerce_str(content.get("raw"))
    if body_text is None:
        # Fallback for ``pullrequest:created`` / ``pullrequest:updated``:
        # the title carries the human-readable summary and is sufficient
        # for the ``[bot:`` regex fallback.
        pr = raw_payload.get("pullrequest")
        if isinstance(pr, Mapping):
            body_text = _coerce_str(pr.get("title"))

    repo_slug: str | None = None
    repo = raw_payload.get("repository")
    if isinstance(repo, Mapping):
        # Prefer ``full_name`` (``workspace/repo``) since that's how
        # ``departments.json`` keys Bitbucket repos.
        repo_slug = _coerce_str(repo.get("full_name"))
        if repo_slug is None:
            repo_slug = _coerce_str(repo.get("slug"))

    pr_id: int | None = None
    pr = raw_payload.get("pullrequest")
    if isinstance(pr, Mapping):
        pr_id = _coerce_int(pr.get("id"))

    return WebhookEvent(
        provider="bitbucket",
        event_type=event_type,
        delivery_id=delivery_id,
        actor_account_id=actor_account_id,
        body_text=body_text,
        project_key=None,
        repo_slug=repo_slug,
        issue_key=None,
        pr_id=pr_id,
        raw_payload=raw_payload,
    )


# ---------------------------------------------------------------------------
# WebhookFilterChain - orchestration skeleton
# ---------------------------------------------------------------------------


class WebhookFilterChain:
    """Deterministic filter chain skeleton for webhook events.

    The chain is stateful only insofar as it stores the callbacks
    supplied at construction time. :meth:`evaluate` itself is pure: it
    accepts a :class:`WebhookEvent`, optionally invokes the callbacks
    in the order defined by the filter pipeline, and returns a
    :class:`FilterDecision`. No I/O is performed by the chain itself -
    every external interaction (Vault, Postgres, dept registry) goes
    through one of the callbacks.

    The default pass decision is
    ``FilterDecision(action="pass", reason="filter_chain_pass")`` when
    no stage drops, bypasses, or rejects the event.
    """

    __slots__ = (
        "_verify_hmac",
        "_resolve_dept",
        "_bot_account_ids",
        "_is_processed",
        "_mention_set_for",
        "_iter_count_for",
        "_reporter_for",
        "_burst_window",
        "_burst_register",
    )

    def __init__(
        self,
        *,
        verify_hmac: VerifyHmac,
        resolve_dept: ResolveDept,
        bot_account_ids: BotAccountIds,
        is_processed: IsProcessed,
        mention_set_for: MentionSetFor,
        iter_count_for: IterCountFor,
        reporter_for: ReporterFor,
        burst_window: timedelta = _DEFAULT_BURST_WINDOW,
        burst_register: BurstRegister | None = None,
    ) -> None:
        """Wire the chain's collaborators.

        All callbacks are required keyword arguments so that
        construction sites in ``automation_service/app.py`` (and in
        tests) are explicit about which collaborator is being supplied.
        ``burst_window`` is the only optional parameter; it defaults to
        3 seconds.

        Parameters
        ----------
        verify_hmac:
            Pure function that returns ``True`` iff the event's
            HMAC signature matches the per-dept secret in Vault
            implementation.
        resolve_dept:
            Maps the event's ``project_key`` (Jira) or ``repo_slug``
            (Bitbucket) to a ``dept_id``; ``None`` when no department
            owns the project / repo.
        bot_account_ids:
            Lazily-evaluated snapshot of all bot account ids registered
            across departments. Re-invoked on every :meth:`evaluate`
            call so registry refreshes propagate without restarting
            the chain.
        is_processed:
            Probe against the ``processed_events`` Postgres table; the
            replay-dedup stage calls this with the event's
            ``delivery_id``.
        mention_set_for:
            Maps an ``issue_key`` to the set of accounts mentioned by
            the bot in that issue.
        iter_count_for:
            Returns the current iteration count for an ``issue_key``.
            Used by the first-iter exception.
        reporter_for:
            Returns the reporter ``account_id`` for an ``issue_key``;
            used by the first-iter exception.
        burst_window:
            Comment-burst debounce window (default 3 seconds). The
            actual coalescing logic lives in the burst window.
        burst_register:
            Optional adapter callback that wires the chain's
            ``_stage_burst_debounce`` stage to a
            :class:`~automation_service.burst_window.BurstWindow`
            instance. When ``None`` (default) the burst stage is
            skipped entirely - useful for unit tests of unrelated
            stages and for the period before the FastAPI router
            instantiates the singleton ``BurstWindow``.
            Production deployments wire this callback at startup so
            comment bursts on the same ``issue_key`` are coalesced
            with the accumulated delivery ids.
        """

        if burst_window.total_seconds() < 0:
            # Defensive: a negative window would silently disable the
            # debounce stage. We reject it at construction time so
            # misconfiguration surfaces immediately rather than at the
            # first event delivery.
            raise ValueError(
                "burst_window must be non-negative; "
                f"got {burst_window!r}"
            )

        self._verify_hmac = verify_hmac
        self._resolve_dept = resolve_dept
        self._bot_account_ids = bot_account_ids
        self._is_processed = is_processed
        self._mention_set_for = mention_set_for
        self._iter_count_for = iter_count_for
        self._reporter_for = reporter_for
        self._burst_window = burst_window
        self._burst_register = burst_register

    # -----------------------------------------------------------------
    # Public properties - handy for tests and for the router
    # so it can introspect the configured window without poking private
    # attributes.
    # -----------------------------------------------------------------

    @property
    def burst_window(self) -> timedelta:
        """Configured comment-burst debounce window."""

        return self._burst_window

    # -----------------------------------------------------------------
    # Stage helpers (replay_dedup, mention_filter,
    # first_iter_exception, streamlit_bypass)
    # -----------------------------------------------------------------
    # # Each ``_stage_*`` method below is a small *pure* helper: it takes
    # a :class:`WebhookEvent`, consults at most one chain callback, and
    # returns a :class:`FilterDecision` if the stage produced a verdict
    # - or ``None`` to mean "this stage did not fire, fall through to
    # the next stage". Splitting the chain like this keeps the unit
    # tests in ``tests/unit/test_webhook_filter_stages.py`` focused on
    # one stage at a time and lets the property tests in
    # ``tests/property/test_webhook_predicates.py`` enumerate every
    # combination without spinning up the entire chain.
    # # The stages are intentionally synchronous: the callbacks are pure
    # Python predicates wired to in-memory caches at the call site
    # (the FastAPI router materialises ``processed_events`` /
    # ``mention_set`` lookups before invoking the chain). When a
    # callback eventually needs an ``await`` (e.g. an async Postgres
    # probe in :func:`is_processed`) the chain will switch to ``async``
    # in lockstep with the router; the test contract stays unchanged.

    # -----------------------------------------------------------------
    # HMAC, department resolution, and loop-guard stages
    # -----------------------------------------------------------------
    # # The two HTTP-level failure stages (``_stage_verify_hmac`` and
    # ``_stage_resolve_dept``) surface their drop reason via raised
    # exceptions because the router needs distinct HTTP status codes
    # (401 / 400) - not a generic 200 ``"drop"``. The loop-guard stage
    # uses the standard :class:`FilterDecision` drop verdict because
    # its policy is "acknowledge but do not propagate" (HTTP 200 with
    # no signal dispatched), exactly mirroring the audit-only drops
    # the chain returns from the mid-chain stages above.

    def _stage_verify_hmac(self, event: WebhookEvent) -> None:
        """Reject the event if its HMAC signature is invalid.

        The runtime ``verify_hmac`` callback wires through to the
        foundation helper :func:`vault_client.verify_webhook_hmac`,
        which itself reads the per-department secret from
        ``vault:webhooks/<provider>/<dept_id>`` and applies the 1h
        rotation overlap window. The chain only
        sees the boolean result, which keeps replay-determinism
        guarantees intact: the verifier's own time-dependent overlap
        comparisons are encapsulated behind the callback.

        On failure this stage **raises** :class:`WebhookHmacInvalidError`
        rather than returning a :class:`FilterDecision`. The router
        catches that exception, writes a
        :data:`REASON_WEBHOOK_HMAC_INVALID` audit row, and returns
        HTTP 401. Surfacing the failure as an exception keeps the
        chain's ``"drop" | "pass"`` verdict cleanly mapped to HTTP
        200 / 202 - every other status code uses a dedicated
        exception type.

        Parameters
        ----------
        event:
            The normalised webhook event whose HMAC signature is
            being verified. The chain forwards the entire event to
            the callback so the verifier can recompute the digest
            against the exact ``raw_payload`` bytes it received.

        Raises
        ------
        WebhookHmacInvalidError
            When the ``verify_hmac`` callback returns ``False``.
        """

        if not self._verify_hmac(event):
            raise WebhookHmacInvalidError()

    def _stage_resolve_dept(self, event: WebhookEvent) -> None:
        """Reject the event if no department owns its project / repo.

        The ``resolve_dept`` callback consults
        ``departments.json`` (or its in-memory mirror) to map the
        event's :attr:`WebhookEvent.project_key` (Jira) or
        :attr:`WebhookEvent.repo_slug` (Bitbucket) to a ``dept_id``.
        If the lookup yields ``None`` - i.e. the project / repo is
        not registered with any department - the event cannot be
        scoped to a Vault secret, an audit dept_id, or a workflow
        capability gate, so we fail loudly rather than silently
        dropping it.

        On failure this stage **raises** :class:`WebhookDeptUnresolvedError`.
        The router catches that exception, writes a
        :data:`REASON_WEBHOOK_DEPT_UNRESOLVED` audit row, and returns
        HTTP 400. The same exception type is used for both Jira and
        Bitbucket dialects: the router does not need to distinguish
        between ``project_key`` and ``repo_slug`` misconfiguration at
        the HTTP layer because the operator remediation is identical
        (add the missing entry to the dept's
        ``jira_project_keys[]`` / ``bitbucket_repos[]`` registry).

        Parameters
        ----------
        event:
            The normalised webhook event whose dept membership is
            being resolved.

        Raises
        ------
        WebhookDeptUnresolvedError
            When the ``resolve_dept`` callback returns ``None``.
        """

        if self._resolve_dept(event) is None:
            raise WebhookDeptUnresolvedError()

    def _stage_loop_guard(
        self, event: WebhookEvent
    ) -> FilterDecision | None:
        """Drop events authored by a registered bot.

        The bot self-action loop guard operates in two layers:

        1. **Actor-id match** - when ``actor_account_id`` is present
           on the event, the chain checks whether it is in the union
           of every department's ``bot.<service>.account_id`` (the
           ``bot_account_ids()`` callback returns this flat union).
           A hit means the event is the bot reacting to its own
           write - drop with reason
           :data:`REASON_LOOP_GUARD_DROPPED`. Cross-department bot
           activity (a bot in dept A commenting on a dept B issue)
           still triggers the drop because the predicate operates on
           the union, not on the per-dept registry.

        2. **Body-text fallback** - when ``actor_account_id`` is
           ``None`` the chain falls back to a regex scan of
           ``body_text`` for the ``[bot:`` prefix at the start of the
           line (with optional leading whitespace). This matches
           comments the bot has written under a previous account
           scheme that no longer surfaces in the actor field; a
           regex hit drops the event with reason
           :data:`REASON_LOOP_GUARD_REGEX_DROPPED`. Distinguishing
           the two reasons in the audit log lets operators tell
           registry hits apart from regex hits, which surface the
           kind of payload-shape issues that warrant runbook follow-up.

        Decision rules in evaluation order
        ----------------------------------

        ::

            if actor_account_id is not None:
                if actor_account_id in bot_account_ids():
                    drop "loop_guard_dropped"
                else:
                    pass through  # legitimate human / 3rd-party actor
            else:  # actor_account_id is None
                if body_text matches BOT_PREFIX_REGEX:
                    drop "loop_guard_regex_dropped"
                else:
                    pass through  # system event with no bot footprint

        The empty / boot-time edge case (``bot_account_ids()`` returns
        an empty set) falls through naturally - the membership check
        returns ``False`` and the event proceeds to the next stage.
        This preserves the invariant that an empty registry never
        silently drops every webhook.

        Parameters
        ----------
        event:
            The normalised webhook event under consideration.

        Returns
        -------
        FilterDecision | None
            ``FilterDecision(action="drop", ...)`` for both the
            actor-id and the regex match cases; ``None`` otherwise.
        """

        actor = event.actor_account_id

        if actor is not None:
            # Re-fetch the snapshot on every event so registry
            # refreshes (a new department booting, a bot rotation)
            # propagate without rebuilding the chain. The callback
            # contract guarantees the return value is a frozenset
            # so the membership check stays O(1).
            if actor in self._bot_account_ids():
                return FilterDecision(
                    action="drop",
                    reason=REASON_LOOP_GUARD_DROPPED,
                    coalesced_with=(),
                )
            # Actor is present but is not a bot - the event survives
            # the loop guard regardless of body text. We deliberately
            # do not consult the regex fallback here because a known
            # human author writing ``[bot: ...]`` quotation in their
            # comment must not be treated as bot output.
            return None

        # Actor is missing - fall back to the body-text regex.
        # ``BOT_PREFIX_REGEX`` is anchored to the start of the line
        # with optional leading whitespace, so the
        # pattern only matches comments that lead with ``[bot:`` and
        # not arbitrary in-line uses (``Bot replied with [bot:hear]``
        # would not match this regex; the legitimate ``[bot:hear]``
        # bypass is handled later by the streamlit-bypass stage).
        if event.body_text is not None and BOT_PREFIX_REGEX.search(
            event.body_text
        ):
            return FilterDecision(
                action="drop",
                reason=REASON_LOOP_GUARD_REGEX_DROPPED,
                coalesced_with=(),
            )

        return None

    @staticmethod
    def _has_streamlit_bypass_tag(body_text: str | None) -> bool:
        """Return True if *body_text* contains the ``[bot:hear]`` tag.

        Implements the etiquette tag detection used by
        :meth:`_stage_streamlit_bypass`. Exposed as a static helper so
        tests can exercise the predicate without constructing a
        :class:`WebhookFilterChain` instance and so the FastAPI router
        can pre-check the tag before deciding whether to enrich the
        event with mention-set lookups.

        The match is case-insensitive against the whole body so that
        editors / clients which auto-capitalise or surround the tag
        with surrounding markdown still trigger the bypass. ``None``
        and empty bodies short-circuit to ``False``.
        """

        if not body_text:
            return False
        return _STREAMLIT_BYPASS_TAG_RE.search(body_text) is not None

    def _stage_streamlit_bypass(
        self, event: WebhookEvent
    ) -> FilterDecision | None:
        """``[bot:hear]`` tag bypasses the rest of the chain.

        This stage runs **before** :meth:`_stage_replay_dedup` so a
        Streamlit inline reply that arrives twice (network retry,
        operator double-click) is still honoured: replay-dedup would
        otherwise mark the duplicate delivery as already-processed
        and drop the user's reply.

        The ordering is documented in the module docstring's filter
        diagram.

        Returns
        -------
        FilterDecision | None
            ``FilterDecision(action="pass", reason=...)`` when the
            tag is present; ``None`` otherwise. Pass-through events
            keep the chain marching to the next stage.
        """

        if self._has_streamlit_bypass_tag(event.body_text):
            return FilterDecision(
                action="pass",
                reason=REASON_STREAMLIT_INLINE_REPLY_WITH_BYPASS,
                coalesced_with=(),
            )
        return None

    def _stage_replay_dedup(
        self, event: WebhookEvent
    ) -> FilterDecision | None:
        """Drop events whose ``delivery_id`` was already processed.

        Idempotency anchor: the
        ``processed_events`` Postgres table holds every successfully
        accepted ``delivery_id``; if Atlassian retries the webhook
        (their default policy is "deliver at least once"), the second
        attempt lands here and is dropped with audit reason
        :data:`REASON_DUPLICATE_EVENT_DROPPED`.

        The ``is_processed`` callback is consulted with the **raw**
        ``delivery_id`` - no hashing or normalisation - because the
        caller is responsible for choosing the canonical idempotency
        key (``X-Atlassian-Webhook-Identifier`` for Jira,
        ``X-Request-UUID`` for Bitbucket).

        Returns
        -------
        FilterDecision | None
            ``FilterDecision(action="drop", reason="duplicate_event_dropped")``
            when ``is_processed`` returns ``True``; ``None`` otherwise.
        """

        if self._is_processed(event.delivery_id):
            return FilterDecision(
                action="drop",
                reason=REASON_DUPLICATE_EVENT_DROPPED,
                coalesced_with=(),
            )
        return None

    def _stage_mention_filter(
        self, event: WebhookEvent
    ) -> FilterDecision | None:
        """Drop unauthorised comments unless the first-iter
        exception applies.

        The two checks are merged into a single method (rather than
        two stages in sequence) because the first-iter exception's
        purpose is explicitly to *bypass* the mention filter. Running
        them as separate stages would risk reordering the precedence
        and either letting an unauthorised comment through (filter
        runs before exception) or dropping a legitimate first-iter
        comment (exception runs before filter and never sees iter > 1
        comments).

        Decision rules in evaluation order
        ----------------------------------

        1. **Scope** - only ``jira:issue_commented`` events are
           subject to the mention filter; every other event type
           passes through unchanged. ``issue_key`` must be present
           because both the reporter check and the mention-set
           lookup are keyed on it; comment events without an
           ``issue_key`` never originate from a real Jira webhook,
           so we conservatively treat them as out-of-scope.
        2. **First-iter exception** - if ``iter_count == 1``
           and the actor matches the issue reporter, the chain
           returns ``pass`` with reason
           :data:`REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION`. This
           lets the reporter trigger the bot on the very first
           iteration without first having to be mentioned by the
           bot (which would be impossible - the bot has not yet
           commented).
        3. **Mention filter** - if ``iter_count > 1`` and the
           actor is not in the bot-mentioned set for the issue, the
           chain returns ``drop`` with reason
           :data:`REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR`.
        4. **Otherwise** - the comment is in scope (mention set hit,
           or ``iter_count == 1`` with a non-reporter actor on a
           freshly-mentioned issue), and the chain returns ``None``
           so the next stage (burst debounce / signalWithStart) can
           run.

        Iter semantics
        --------------

        ``iter_count_for(issue_key)`` returns the **current** iteration
        count for the issue. The convention used by the agent runner
        workflow is:

        * ``0`` - the issue has never been processed; this should
          not happen in practice for a comment event because
          ``jira:issue_created`` would have advanced the counter to
          ``1`` first. We treat ``0`` the same as ``1`` for the
          check so a freshly-created issue's first comment is
          honoured.
        * ``1`` - the workflow is in iter 1; the exception applies if the
          actor matches the reporter.
        * ``≥ 2`` - the exception does not apply and the mention filter is
          enforced.

        Returns
        -------
        FilterDecision | None
            See the decision rules above.
        """

        # Stage scope - non-comment events fall through.
        if event.event_type != JIRA_COMMENT_EVENT_TYPE:
            return None
        if event.issue_key is None:
            # Defensive: Atlassian always populates ``issue.key`` for a
            # ``jira:issue_commented`` event. If it ever arrives empty
            # we cannot evaluate the comment filters deterministically, so we let
            # the event flow on; the FastAPI router has already
            # validated the schema upstream.
            return None

        actor = event.actor_account_id
        iter_count = self._iter_count_for(event.issue_key)

        # First-iter exception. The reporter check runs first so
        # the chain produces a stable ``mention_filter_first_iter_exception``
        # audit label even when the reporter happens to also be in the
        # mention set (which would otherwise short-circuit to a plain
        # pass with no exception label).
        if iter_count <= 1 and actor is not None:
            reporter = self._reporter_for(event.issue_key)
            if reporter == actor:
                return FilterDecision(
                    action="pass",
                    reason=REASON_MENTION_FILTER_FIRST_ITER_EXCEPTION,
                    coalesced_with=(),
                )

        # Mention filter. Only enforced for iter > 1; iter == 1 with
        # a non-reporter actor still
        # falls through (which is intentional - first-iter activity
        # from arbitrary commenters is allowed once, the next iter
        # tightens to mention-set membership).
        if iter_count > 1:
            mention_set = self._mention_set_for(event.issue_key)
            if actor is None or actor not in mention_set:
                return FilterDecision(
                    action="drop",
                    reason=REASON_COMMENT_IGNORED_UNAUTHORIZED_ACTOR,
                    coalesced_with=(),
                )

        return None

    def _stage_burst_debounce(
        self, event: WebhookEvent
    ) -> FilterDecision | None:
        """Coalesce same-``issue_key`` events inside a 3s window.

        This is the **final** filter stage before the chain returns
        ``filter_chain_pass``. It consults the chain's
        :data:`BurstRegister` callback (typically wired to a
        :class:`~automation_service.burst_window.BurstWindow`
        singleton owned by the FastAPI app); if no callback is
        configured the stage is a no-op and the chain passes the
        event through unchanged.

        Decision rules
        --------------

        * No ``burst_register`` callback configured  ``None``
          (skip the stage).
        * Callback returns ``None``  ``None`` (the callback decided
          the event is out of scope for the burst window - for
          instance because it has no ``issue_key``).
        * Callback returns ``BurstRegisterResult(decision="coalesce_emit")``
           ``None`` (the event opened a fresh window; the chain
          falls through to ``filter_chain_pass`` so the FastAPI
          router dispatches the event normally - the eventual flush
          will carry the coalesced delivery_ids).
        * Callback returns ``BurstRegisterResult(decision="coalesce_dropped")``
           :class:`FilterDecision` with ``action="drop"``,
          ``reason="burst_coalesced"``, and ``coalesced_with`` set to
          the running delivery_id list. The router replies ``200 OK``
          and writes the audit row; the buffered payload remains in
          the :class:`BurstWindow` until a sweeper invokes
          :meth:`~automation_service.burst_window.BurstWindow.flush_window`.

        Why this stage runs last
        ------------------------

        This stage must observe the *real* deliveries that survived
        every other policy filter - running burst-debounce earlier
        would coalesce events that the mention filter or replay-dedup
        would otherwise have dropped, inflating the
        ``coalesced_with`` audit list with deliveries that never
        produced a workflow signal. Running the stage last keeps the
        coalesce semantics tight: every delivery in the window is
        one that *would* have gone to ``signalWithStart``.

        Returns
        -------
        FilterDecision | None
            See the decision rules above.
        """

        if self._burst_register is None:
            return None

        result = self._burst_register(event)
        if result is None:
            return None

        if result.decision == "coalesce_dropped":
            return FilterDecision(
                action="drop",
                reason=REASON_BURST_COALESCED,
                coalesced_with=result.coalesced_with,
            )

        # ``coalesce_emit`` - fresh window; let the chain pass the
        # event through. The router dispatches normally; the
        # ``coalesced_with`` audit list is empty for this delivery
        # because no other deliveries have been merged yet.
        return None

    # -----------------------------------------------------------------
    # Evaluation
    # -----------------------------------------------------------------

    def evaluate(self, event: WebhookEvent) -> FilterDecision:
        """Run *event* through the filter chain and return the verdict.

        Stage order
        -----------

        ::

            verify_hmac        # raises WebhookHmacInvalidError
            resolve_dept       # raises WebhookDeptUnresolvedError
            loop_guard         # actor-id + [bot: regex fallback
            streamlit_bypass   # runs BEFORE replay_dedup
            replay_dedup
            mention_filter     # first-iter exception merged in
            burst_debounce
            (default pass)

        The two HTTP-level failure stages (``verify_hmac``,
        ``resolve_dept``) raise dedicated exceptions instead of
        returning :class:`FilterDecision` drops because their HTTP
        semantics (401 / 400) cannot be expressed by the chain's
        ``"drop" | "pass"`` verdict alphabet (which maps to 200 /
        202 in the router). The router catches those exceptions and
        writes the matching audit row.

        ``loop_guard`` runs immediately after dept resolution so that
        a bot self-action in an unregistered project still surfaces
        as ``webhook_dept_unresolved`` (the more actionable signal
        for operators) rather than ``loop_guard_dropped``. This
        preserves the filter-chain decision order.

        The mid-chain filters deal with comment-event policy: the
        ``[bot:hear]`` bypass, the replay-dedup against the
        ``processed_events`` table, and the mention filter with the
        first-iter exception merged in.

        Why ``streamlit_bypass`` runs before ``replay_dedup``
        ----------------------------------------------------

        The ``[bot:hear]`` etiquette tag is the only signal that a comment came
        from the bot's own Streamlit UI; any retry of that delivery
        (network blip, browser double-submit, Atlassian re-fire) must
        still be honoured because the user is the originator. Running
        the bypass *before* replay-dedup means the second delivery's
        duplicate ``delivery_id`` does not silently swallow the user's
        reply. The cost is an extra workflow signal in the rare
        retry case, which is the correct trade-off.

        Why the first-iter exception lives inside mention_filter
        ---------------------------------------------------------

        The first-iter exception is the exception path for the mention
        filter: its purpose is to let the reporter trigger the bot on
        iter 1 before the bot has had a chance to mention anyone.
        Modelling it as a separate stage in front of the mention filter would
        leak iter-aware logic into a position that runs for every
        event type, not just comments. Folding the exception into the
        mention filter (as a precondition that returns ``pass`` when
        iter == 1 and actor == reporter) keeps both rules co-located and makes
        the property tests trivial to enumerate.

        Parameters
        ----------
        event:
            The normalised webhook event produced by
            :func:`normalize_jira_event` or
            :func:`normalize_bitbucket_event`.

        Returns
        -------
        FilterDecision
            A deterministic verdict. The chain returns ``("pass",
            "filter_chain_pass")`` when no stage fires.
        """

        # Defensive type guard: callers must pass a properly normalised
        # event. We don't accept dicts or raw payloads here so the
        # downstream stages can assume the contract.
        if not isinstance(event, WebhookEvent):
            raise TypeError(
                "WebhookFilterChain.evaluate expects a WebhookEvent; "
                f"got {type(event).__name__}"
            )

        # HTTP-level stages: verify_hmac and resolve_dept raise
        # dedicated exceptions on failure so the FastAPI router can
        # translate them into HTTP 401 / 400 with the matching audit
        # reason. The loop_guard stage returns a standard
        # ``FilterDecision`` drop verdict (HTTP 200) that distinguishes
        # actor-id matches from regex-fallback matches in the audit
        # log.
        self._stage_verify_hmac(event)
        self._stage_resolve_dept(event)
        decision = self._stage_loop_guard(event)
        if decision is not None:
            return decision

        # Run streamlit_bypass first so [bot:hear]
        # short-circuits replay_dedup and mention_filter both.
        decision = self._stage_streamlit_bypass(event)
        if decision is not None:
            return decision

        decision = self._stage_replay_dedup(event)
        if decision is not None:
            return decision

        decision = self._stage_mention_filter(event)
        if decision is not None:
            return decision

        # Burst debounce runs last so it only coalesces
        # deliveries that would otherwise have reached
        # ``signalWithStart``. ``coalesce_dropped`` returns a drop
        # verdict with ``coalesced_with`` populated; ``coalesce_emit``
        # falls through to ``filter_chain_pass``.
        decision = self._stage_burst_debounce(event)
        if decision is not None:
            return decision

        return FilterDecision(
            action="pass",
            reason=REASON_FILTER_CHAIN_PASS,
            coalesced_with=(),
        )
