"""``POST /webhooks/jira`` and ``POST /webhooks/bitbucket``.

* Wire the
  :class:`~automation_service.webhook_filters.WebhookFilterChain` to a
  pair of FastAPI endpoints. The chain runs end-to-end (HMAC verify,
  dept resolve, loop guard, replay dedup, mention / first-iter / V12
  bypass, burst debounce); its verdict drives the HTTP response and
  audit-log emission. On a ``"pass"`` decision the handler claims the
  ``delivery_id`` against ``automation.processed_events`` and dispatches
  the workflow via :func:`temporal_shared.start_helper.start_workflow_idempotent`
  (``signalWithStart`` semantics).

  Response code matrix:

  ===========================  ==========================================  =======
  Decision                     Audit reason                                Status
  ===========================  ==========================================  =======
  HMAC fail                    ``webhook_hmac_invalid``                    401
  Dept resolve fail            ``webhook_dept_unresolved``                 400
  Filter chain drop (any)      ``loop_guard_dropped`` / ``..._regex_..``   200
                               / ``duplicate_event_dropped`` /
                               ``comment_ignored_unauthorized_actor`` /
                               ``burst_coalesced``
  Filter chain pass + dispatch ``webhook_workflow_started`` /              202
                               ``webhook_workflow_already_started``
  Unsupported event type       ``webhook_event_ignored``                   200
  ===========================  ==========================================  =======

* Event-type allowlists. Jira
  endpoints accept ``jira:issue_created``, ``jira:issue_assigned``,
  ``jira:issue_updated``, ``jira:issue_commented``. Bitbucket
  endpoints accept ``pullrequest:created``, ``pullrequest:commented``,
  ``pullrequest:updated``. Anything outside those sets is silently
  dropped with audit ``webhook_event_ignored`` and HTTP 200.

  Bitbucket ``pullrequest:fulfilled`` (PR merged) is **explicitly
  not** in the allowlist either: the bot itself merges nothing (banned tool list) and any merge event we
  observe is therefore a human action that has already settled. The
  loop-guard column in the design table flags the merge as a drop;
  this endpoint surfaces that drop as ``webhook_event_ignored`` so
  operators can distinguish merges from arbitrary unknown events
  via the audit ``payload.event_type`` field.

This module is the **public HTTP entry point** for Atlassian
webhooks; the older ``automation_service/webhooks_handlers.py`` (Jira
``issue_created`` / ``issue_commented`` hand-rolled chain from the
legacy handler) and the legacy ``src/webhooks/{jira,bitbucket}.py``
(Phase-1 stand-alone handlers) remain mounted in parallel during the
migration so existing deployments do not break — but new wiring goes
through these endpoints.

"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Awaitable,
    Callable,
    Final,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from temporal_shared.identifiers import (
    InvalidIssueKeyError,
    InvalidSlugError,
    automation_workflow_id_jira,
    bitbucket_pr_workflow_id,
)
from temporal_shared.start_helper import (
    StartResult,
    SupportsStartWorkflow,
    start_workflow_idempotent,
)
from temporal_shared.messages import (
    AutomationWorkflowInput,
    WebhookEvent as TemporalWebhookEvent,
)
from temporal_shared.workflow_registry import task_queue_for

from ..processed_events import ProcessedEventsRepo
from ..webhook_filters import (
    FilterDecision,
    JIRA_EVENT_TYPES,
    REASON_LOOP_GUARD_DROPPED,
    WebhookDeptUnresolvedError,
    WebhookEvent,
    WebhookFilterChain,
    WebhookHmacInvalidError,
    normalize_bitbucket_event,
    normalize_jira_event,
)

# License-cap middleware. Imported lazily
# at module-init so existing call sites keep working when the
# middleware module is unavailable (e.g. older deployments that have
# not yet rolled out the license-cap migration). Production wiring
# always provides a :class:`LicenseCapEnforcer` callable; tests that
# do not exercise the cap path leave it ``None`` and the enforcement
# stage short-circuits.
try:  # pragma: no cover — import guarded for legacy deployments
    from middleware.license_cap import (  # type: ignore[import-not-found]
        BotLicenseCapExceededError,
    )
except ImportError:  # pragma: no cover — optional dependency
    class BotLicenseCapExceededError(RuntimeError):  # type: ignore[no-redef]
        """Fallback stub used when the middleware module is unavailable.

        Kept structurally compatible with the real exception so the
        ``isinstance`` check in :func:`_dispatch_pass` resolves
        identically regardless of which symbol the wiring code imports.
        Production deployments always import the real class.
        """

        limit_type: str = ""
        current: Any = 0
        max: Any = 0
        license_id: str | None = None
        dept_id: str = ""
        issue_key: str | None = None

__all__ = [
    "WebhooksEndpointDeps",
    "router",
    "JIRA_SUPPORTED_EVENTS",
    "BITBUCKET_SUPPORTED_EVENTS",
    "BITBUCKET_LOOP_GUARD_EVENTS",
    "LicenseCapEnforcer",
    "JiraAckCommentPoster",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — event allowlists
# ---------------------------------------------------------------------------

#: Jira webhook event types this endpoint dispatches to a workflow.
#: Mirrors :data:`automation_service.webhook_filters.JIRA_EVENT_TYPES`
#: so the two stay in lockstep; we still expose the set here so the
#: handler can decide *before* normalising whether to drop the event
#: with ``webhook_event_ignored``.
JIRA_SUPPORTED_EVENTS: Final[frozenset[str]] = frozenset(JIRA_EVENT_TYPES)

#: Bitbucket webhook event types this endpoint dispatches. The merge
#: event (``pullrequest:fulfilled``) is intentionally excluded so the
#: handler can short-circuit to ``loop_guard_dropped`` without going
#: through HMAC verification — a merge fired by the bot itself would
#: otherwise re-enter the chain and burn HMAC + dept-resolve cycles.
BITBUCKET_SUPPORTED_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "pullrequest:created",
        "pullrequest:commented",
        "pullrequest:updated",
    }
)

#: Bitbucket events that are **recognised** but always loop-guarded.
#: ``pullrequest:fulfilled`` is the canonical example: the merge tool
#: is banned for the bot, so any ``fulfilled`` event we receive is a
#: human action that has already taken effect — re-running the chain
#: would only invite a self-trigger. Surfacing this as a distinct
#: audit reason (``loop_guard_dropped``) lets operators tell merges
#: apart from arbitrary unknown events (``webhook_event_ignored``).
BITBUCKET_LOOP_GUARD_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "pullrequest:fulfilled",
    }
)

#: Provider → header name carrying the HMAC signature. The two
#: dialects diverge here: Jira ships
#: ``X-Atlassian-Webhook-Signature`` (Atlassian Cloud's standard),
#: Bitbucket ships ``X-Hub-Signature`` (the GitHub-style envelope).
_HEADER_HMAC_BY_PROVIDER: Final[Mapping[str, str]] = MappingProxyType(
    {
        "jira": "X-Atlassian-Webhook-Signature",
        "bitbucket": "X-Hub-Signature",
    }
)

#: Provider → header name carrying the idempotency delivery id.
_HEADER_DELIVERY_BY_PROVIDER: Final[Mapping[str, str]] = MappingProxyType(
    {
        "jira": "X-Atlassian-Webhook-Identifier",
        "bitbucket": "X-Request-UUID",
    }
)

#: Bitbucket carries the event type in the ``X-Event-Key`` header
#: rather than the payload body. Defined as a constant so the
#: handler and the tests reference the same literal.
_HEADER_BITBUCKET_EVENT_KEY: Final[str] = "X-Event-Key"

#: Temporal task queue used for ``AutomationWorkflow``. Matches the
#: existing wiring in ``webhooks_handlers.py`` so a single worker can
#: drain both endpoints.
#: Temporal workflow type started for incoming events. The workflow
#: itself is registered by ``automation-worker``; the webhook only
#: needs the name and resolves its queue from the shared registry.
_WORKFLOW_NAME: Final[str] = "AutomationWorkflow"
_TASK_QUEUE: Final[str] = task_queue_for(_WORKFLOW_NAME)

#: Audit ``actor_id`` for endpoint-emitted events. The handler is the
#: actor; the role is always ``"system"`` per
#: background-process audit rows.
_AUDIT_ACTOR_ID: Final[str] = "automation-service.webhook"

#: Audit reason emitted when the event-type allowlist drops an event
#: Distinct from the chain's drop reasons so operators
#: can tell "we accept this URL but not that event" apart from
#: "the chain decided to drop a recognised event".
_REASON_WEBHOOK_EVENT_IGNORED: Final[str] = "webhook_event_ignored"

#: Audit reason emitted when the chain passes and ``signalWithStart``
#: produces a fresh execution.
_REASON_WORKFLOW_STARTED: Final[str] = "webhook_workflow_started"

#: Audit reason emitted when the chain passes but ``signalWithStart``
#: collapsed to the existing execution (Temporal native idempotency).
_REASON_WORKFLOW_ALREADY_STARTED: Final[str] = "webhook_workflow_already_started"

#: Audit reason emitted when ``processed_events.claim`` returns
#: ``False`` *after* the chain has already passed. The chain's
#: ``replay_dedup`` stage normally catches the duplicate first; this
#: branch only fires on a tight race between two concurrent webhook
#: deliveries that both observed an empty ``processed_events`` table
#: simultaneously.
_REASON_WEBHOOK_CLAIM_DUPLICATE: Final[str] = "webhook_claim_duplicate"

#: Audit reason emitted when ``signalWithStart`` raises an unexpected
#: error and the handler had to release the ``processed_events``
#: claim so Atlassian's webhook retry can re-process the delivery
#: after a failed start.
_REASON_WORKFLOW_START_FAILED: Final[str] = "webhook_workflow_start_failed"

#: Audit reason emitted when the per-license hard cap blocks a
#: workflow start. The middleware's own
#: ``bot_license_cap_exceeded`` audit captures the cap details; this
#: webhook-layer row records the *delivery* that was rejected so
#: operators can correlate the 429 back to the originating Atlassian
#: webhook envelope (delivery_id, event_type, workflow_id) without a
#: cross-table join.
_REASON_WEBHOOK_BLOCKED_LICENSE_CAP: Final[str] = (
    "webhook_workflow_start_blocked_license_cap"
)


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@runtime_checkable
class _DeptResolverByProjectKey(Protocol):
    """Resolves a Jira project_key to a department id.

    Optional collaborator — required only when the handler wants to
    populate ``dept_id`` on audit rows for Jira webhook events. The
    runtime implementation reads ``automation.department_project_keys``
    (or the in-memory dept registry); tests pass a hand-built mapping.
    """

    async def resolve_jira_dept(self, project_key: str) -> str | None: ...


@runtime_checkable
class _DeptResolverByRepo(Protocol):
    """Resolves a Bitbucket repository slug to a department id.

    See :class:`_DeptResolverByProjectKey`; the same optional contract
    applies for Bitbucket events.
    """

    async def resolve_bitbucket_dept(self, repo_slug: str) -> str | None: ...


#: Async callable enforcing the per-license hard cap. The
#: production binding wraps :func:`middleware.license_cap.enforce_license_cap`
#: with an ``asyncpg.Pool`` and the audit logger so the dispatcher only
#: needs to invoke it with ``(dept_id, issue_key)``. The callable raises
#: :class:`BotLicenseCapExceededError` when any of the three caps
#: (concurrent / daily / monthly_token) is met or exceeded; on a green
#: pass it returns ``None`` and the dispatcher proceeds to claim the
#: delivery and start the workflow.
LicenseCapEnforcer = Callable[[str, str | None], Awaitable[None]]

#: Async callable posting a best-effort acknowledgement comment to the
#: originating Jira issue when a workflow start is blocked by the
#: license cap. The dispatcher calls it with ``(dept_id, issue_key,
#: comment_body)``; the callable is expected to swallow MCP / network
#: errors so the rejection signal is never masked by an audit-side
#: failure. Bitbucket-only flows pass ``None`` for ``issue_key`` and
#: the dispatcher skips the call entirely.
JiraAckCommentPoster = Callable[[str, str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WebhooksEndpointDeps:
    """Collaborators the webhooks router pulls from ``app.state.webhooks``.

    The router itself owns no state; production wiring builds an
    instance of this dataclass during FastAPI startup, while tests
    construct one with hand-rolled fakes. Keeping the dependencies in
    a frozen dataclass mirrors the pattern used by
    :class:`automation_service.api.cancel.CancelEndpointDeps` so every
    new endpoint shares the same wiring contract.

    Attributes
    ----------
    chain:
        The :class:`WebhookFilterChain` already wired with the
        per-stage callbacks (vault HMAC verifier, dept resolver, bot
        registry, ``processed_events`` probe, mention sets, iteration
        counts, reporter resolver, burst window). The router calls
        :meth:`WebhookFilterChain.evaluate` once per request.
    processed_events:
        Repository for the ``automation.processed_events`` idempotency
        table. Used to claim a successful delivery before
        ``signalWithStart`` and to release it on failure.
    workflow_client:
        The Temporal client wrapper exposing ``start_workflow``.
        Forwarded verbatim to
        :func:`start_workflow_idempotent`.
    audit_logger:
        Audit sink for every drop / pass / failure outcome. The
        handler emits exactly one audit row per request.
    jira_dept_resolver / bitbucket_dept_resolver:
        Optional resolvers used to enrich audit rows with the resolved
        ``dept_id`` for the Jira / Bitbucket dialects respectively.
        Both default to ``None``; when omitted the audit ``dept_id``
        is left unset (chain-side dept resolution is still authoritative
        — its result drives the chain's HMAC verifier and is captured
        in the chain decision regardless of these callbacks).
    clock:
        Optional callable returning the current UTC datetime. Defaults
        to :func:`datetime.now` with ``timezone.utc``. Tests inject a
        frozen clock so audit timestamps are deterministic.
    monotonic_clock:
        Optional callable returning a monotonic timestamp in seconds
        (typically :func:`time.monotonic`). Used for the ≤500ms
        latency target enforcement (emitted as
        ``payload.duration_ms`` so operators can monitor the budget
        without bolting on metrics middleware).
    license_cap_enforcer:
        Optional async callable applying the per-license hard cap
        Production wiring binds it to
        :func:`middleware.license_cap.enforce_license_cap` curried
        with the ``asyncpg.Pool`` and audit logger so the dispatcher
        only passes ``(dept_id, issue_key)``. ``None`` (default)
        skips the cap check entirely — used by older deployments and
        by tests that do not exercise the cap path. When set, the
        callable runs *before* :meth:`ProcessedEventsRepo.claim` so a
        rejected delivery does not consume an idempotency slot;
        Atlassian's webhook retry then redelivers once capacity
        frees up (the 429 contract surfaces this expectation in the
        response body).
    jira_ack_comment_poster:
        Optional async callable posting a best-effort acknowledgement
        comment on the originating Jira issue when the cap blocks a
        workflow start. The callable is expected to swallow its own
        errors so a broken MCP path never re-throws into the webhook
        handler. ``None`` (default) skips the comment — Bitbucket-only
        flows or fixtures that do not exercise the comment path leave
        it unset.
    """

    chain: WebhookFilterChain
    processed_events: ProcessedEventsRepo
    workflow_client: SupportsStartWorkflow
    audit_logger: AuditLogger
    jira_dept_resolver: _DeptResolverByProjectKey | None = None
    bitbucket_dept_resolver: _DeptResolverByRepo | None = None
    clock: Callable[[], datetime] | None = None
    monotonic_clock: Callable[[], float] | None = None
    license_cap_enforcer: LicenseCapEnforcer | None = None
    jira_ack_comment_poster: JiraAckCommentPoster | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return current UTC datetime — default for :attr:`WebhooksEndpointDeps.clock`."""

    return datetime.now(timezone.utc)


def _departments_config() -> dict[str, dict[str, Any]]:
    """Load the file-backed department config as a safe fallback."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "departments.json"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return {}
            return {
                str(item.get("id")): dict(item)
                for item in data.get("departments", [])
                if isinstance(item, dict) and item.get("id")
            }
    return {}


def _workflow_input_for_event(
    event: WebhookEvent,
    *,
    dept_id: str,
    trace_id: str,
) -> AutomationWorkflowInput:
    """Build the canonical Temporal input for ``AutomationWorkflow``."""
    config = _departments_config().get(dept_id, {})
    bot = config.get("bot") if isinstance(config.get("bot"), Mapping) else {}
    caps: set[str] = set()
    for service in ("jira", "bitbucket", "confluence"):
        entry = bot.get(service) if isinstance(bot, Mapping) else None
        if isinstance(entry, Mapping) and entry.get("credential_ref"):
            caps.add(service)
    if not caps:
        caps.add(event.provider)
    runner_assigned = any(
        os.environ.get(key, "").strip().lower()
        in {"1", "true", "yes", "on"}
        for key in ("EXECUTION_RUNNER_ASSIGNED", "EXECUTION_RUNNER_AVAILABLE")
    )
    if runner_assigned or os.environ.get("SSH_HOST"):
        caps.add("execution")
    if config.get("web_search_enabled") and os.environ.get(
        "FIRECRAWL_ENABLED", "false"
    ) == "true":
        caps.add("web_search")
    repo_mappings = config.get("repo_mappings") or []
    issue_key = event.issue_key or (
        f"BB-{event.pr_id}" if event.pr_id is not None else event.delivery_id
    )
    raw_event = TemporalWebhookEvent(
        provider=event.provider,
        event_type=event.event_type,
        delivery_id=event.delivery_id,
        actor_account_id=event.actor_account_id,
        body_text=event.body_text,
        project_key=event.project_key,
        repo_slug=event.repo_slug,
        issue_key=event.issue_key,
        pr_id=event.pr_id,
        raw_payload=tuple(event.raw_payload.items()),
    )
    return AutomationWorkflowInput(
        issue_key=issue_key,
        department_id=dept_id,
        available_capabilities=tuple(sorted(caps)),
        available_repos=tuple(
            str(m.get("bitbucket_repo"))
            for m in repo_mappings
            if isinstance(m, Mapping) and m.get("bitbucket_repo")
        ),
        available_spaces=tuple(config.get("confluence_space_keys") or ()),
        default_language=str(config.get("default_language") or "tr"),
        trigger_event=event.event_type,
        raw_event=raw_event,
        trace_id=trace_id,
        notify_on_success=bool(config.get("notify_on_success", False)),
        notify_channels=tuple(config.get("notify_channels") or ()),
        slack_webhook=None,
        notify_email=config.get("notify_email"),
    )


def _make_audit_event(
    *,
    action: str,
    resource: str,
    result: Literal["ok", "denied", "error"],
    dept_id: str | None,
    payload: dict[str, Any] | None,
    now: datetime,
) -> AuditEvent:
    """Construct an :class:`AuditEvent` for the webhook handler.

    Every audit row emitted by this module is system-actor
    (``actor_role="system"``) and uses a stable ``actor_id``. The
    resource is the request path so operators can filter by endpoint.
    """

    return AuditEvent(
        actor_id=_AUDIT_ACTOR_ID,
        actor_role="system",
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,
        timestamp=now,
        payload=payload,
    )


async def _emit_audit(
    audit_logger: AuditLogger, event: AuditEvent
) -> None:
    """Write *event* to the audit log, swallowing errors.

    A broken audit pipeline must not block webhook acknowledgement —
    Atlassian retries deliveries indefinitely on 5xx and we would
    rather degrade audit fidelity than lock the gateway out of a
    transient Postgres outage. The failure is logged locally for
    operator follow-up.
    """

    try:
        await audit_logger.write(event)
    except Exception as exc:  # noqa: BLE001 - best-effort
        _LOG.warning(
            "webhook_audit_write_failed",
            extra={
                "action": event.action,
                "dept_id": event.dept_id,
                "error": type(exc).__name__,
            },
        )


def _build_deps(request: Request) -> WebhooksEndpointDeps | None:
    """Return ``app.state.webhooks`` if the application is wired."""

    deps = getattr(request.app.state, "webhooks", None)
    if deps is None or not isinstance(deps, WebhooksEndpointDeps):
        return None
    return deps


def _safe_json_loads(raw: bytes) -> dict[str, Any] | None:
    """Return parsed JSON object, or ``None`` if invalid."""

    try:
        decoded = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _json_response(
    body: dict[str, Any], *, status_code: int
) -> JSONResponse:
    """Stable JSON response (UTF-8, ASCII-safe)."""

    return JSONResponse(status_code=status_code, content=body)


def _resolve_delivery_id(request: Request, provider: str) -> str:
    """Return the provider-specific delivery_id header, or a fallback.

    Atlassian deliveries always carry their canonical id header;
    fixtures and curl-based smoke tests sometimes omit it, so the
    handler synthesises a stable but unique fallback derived from the
    payload hash via the calling code (we accept an empty header here
    and fall through to the generic empty-string default — the chain's
    ``replay_dedup`` stage will treat the empty string as ``False``
    and the ``processed_events`` table will then claim it).
    """

    header = _HEADER_DELIVERY_BY_PROVIDER[provider]
    value = request.headers.get(header) or ""
    return value


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["webhooks-v3"])


# ---------------------------------------------------------------------------
# Verify-HMAC adapter
# ---------------------------------------------------------------------------
#
# The :class:`WebhookFilterChain.verify_hmac` callback receives a
# :class:`WebhookEvent` and returns ``True`` when the signature is
# valid. The runtime callback wired into the chain reads the raw
# request body + signature from per-request state because the
# ``WebhookEvent`` itself does not carry the HMAC header. The handler
# below stashes the body / signature on the event's ``raw_payload``
# wrapper before invoking the chain so the callback can extract them
# via :func:`_extract_hmac_inputs`.

_HMAC_BODY_KEY: Final[str] = "__webhook_raw_body__"
_HMAC_SIGNATURE_KEY: Final[str] = "__webhook_signature__"


def _extract_hmac_inputs(event: WebhookEvent) -> tuple[bytes, str]:
    """Pull ``(body, signature)`` from *event*'s :attr:`raw_payload` envelope.

    The chain's ``verify_hmac`` callback consumes these to recompute
    the digest. Returning empty strings on miss keeps the helper
    side-effect free; the verifier callback then surfaces an HMAC
    mismatch as ``False`` and the chain raises
    :class:`WebhookHmacInvalidError`.
    """

    raw = event.raw_payload
    body = raw.get(_HMAC_BODY_KEY) if isinstance(raw, Mapping) else None
    signature = (
        raw.get(_HMAC_SIGNATURE_KEY) if isinstance(raw, Mapping) else None
    )
    return (
        body if isinstance(body, (bytes, bytearray)) else b"",
        signature if isinstance(signature, str) else "",
    )


def _augment_payload_with_hmac(
    payload: Mapping[str, Any], *, body: bytes, signature: str
) -> dict[str, Any]:
    """Return a copy of *payload* with HMAC inputs attached.

    The chain's ``verify_hmac`` callback reads them via
    :func:`_extract_hmac_inputs`. Storing the data on the event keeps
    the chain's pure-function contract intact — every input is on the
    event, every output is in the :class:`FilterDecision`.
    """

    augmented: dict[str, Any] = dict(payload)
    augmented[_HMAC_BODY_KEY] = body
    augmented[_HMAC_SIGNATURE_KEY] = signature
    return augmented


# ---------------------------------------------------------------------------
# Workflow_id derivation
# ---------------------------------------------------------------------------


def _workflow_id_for(event: WebhookEvent) -> str | None:
    """Return the canonical Temporal workflow_id for *event*, or ``None``.

    Uses the foundation helpers in
    :mod:`temporal_shared.identifiers` so the format stays in lockstep
    with the workflow registry. Returns ``None`` when the event
    lacks the keys needed to construct an id — the handler then falls
    back to dropping the event with HTTP 400 because we cannot route
    it deterministically.
    """

    if event.provider == "jira":
        if not event.issue_key:
            return None
        try:
            return automation_workflow_id_jira(event.issue_key)
        except InvalidIssueKeyError:
            return None

    # bitbucket workflow id format
    # ``automation-bb-{repo_slug}-pr-{pr_id}``. The ``repo_slug``
    # field on the normalised event is either the Bitbucket Cloud
    # ``full_name`` (``workspace/repo``) or the bare ``slug``. Both
    # collapse to a single dash-joined slug for the workflow id; the
    # underlying :func:`bitbucket_pr_workflow_id` validator accepts
    # any non-empty ``[a-z0-9-]`` slug without leading / trailing /
    # doubled dashes.
    if not event.repo_slug or event.pr_id is None:
        return None
    slug = event.repo_slug.replace("/", "-").lower().strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    if not slug:
        return None
    try:
        return bitbucket_pr_workflow_id(slug, event.pr_id)
    except InvalidSlugError:
        return None


# ---------------------------------------------------------------------------
# Common dispatch path
# ---------------------------------------------------------------------------


async def _dispatch_pass(
    *,
    deps: WebhooksEndpointDeps,
    event: WebhookEvent,
    decision: FilterDecision,
    workflow_id: str,
    dept_id: str | None,
    audit_resource: str,
    started_at_monotonic: float,
    monotonic_clock: Callable[[], float],
    now: datetime,
) -> JSONResponse:
    """Claim the delivery, dispatch the workflow, audit, and respond.

    Encapsulates the "filter chain passed → fire signalWithStart"
    branch so the Jira and Bitbucket entry points stay tiny. The
    sequence is:

    0. **License-cap enforcement** — when
       :attr:`WebhooksEndpointDeps.license_cap_enforcer` is wired and
       a ``dept_id`` was resolved, run the cap helper *before*
       claiming the delivery. A cap breach raises
       :class:`BotLicenseCapExceededError` and we return HTTP 429
       with a structured body, an audit row tagged
       ``webhook_workflow_start_blocked_license_cap``, and a
       best-effort Jira comment so the end user knows the bot is
       throttled rather than ignoring them. The middleware itself
       writes the ``bot_license_cap_exceeded`` audit row with the
       raw cap details — the webhook layer's row records the
       *delivery* metadata (delivery_id, event_type, workflow_id) so
       operators can correlate the 429 back to the originating
       Atlassian envelope without a cross-table join.
    1. ``processed_events.claim`` — ``False`` on race → 200 with
       audit ``webhook_claim_duplicate``.
    2. :func:`start_workflow_idempotent` — on exception, release the
       claim and re-raise so the webhook provider retries.
    3. Audit row tagged with ``webhook_workflow_started`` /
       ``webhook_workflow_already_started`` and the resolved
       ``workflow_id`` + duration.
    4. HTTP 202 with ``workflow_id`` and ``was_existing`` fields.
    """

    delivery_id = event.delivery_id

    # ---- (0) License cap enforcement ------------------------------
    #
    # Runs before the idempotency claim so a rejected delivery does
    # not consume a ``processed_events`` slot. Atlassian's webhook
    # retry will redeliver the same envelope after the cap-window
    # rolls (concurrent workflows finish, day rolls over, monthly
    # budget refreshes), at which point the same delivery_id flows
    # through the chain again and lands a fresh claim.
    if deps.license_cap_enforcer is not None and dept_id is not None:
        try:
            await deps.license_cap_enforcer(dept_id, event.issue_key)
        except BotLicenseCapExceededError as cap_exc:
            return await _reject_for_license_cap(
                deps=deps,
                event=event,
                workflow_id=workflow_id,
                dept_id=dept_id,
                audit_resource=audit_resource,
                exc=cap_exc,
                now=now,
            )

    claimed = await deps.processed_events.claim(delivery_id, event.provider)
    if not claimed:
        # Tight race window: two concurrent deliveries observed an
        # empty processed_events table at the same time, both passed
        # the chain, and only one of them actually inserted the row.
        # The losing caller drops with a distinct audit reason so this
        # case is observable in the audit log.
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                action=_REASON_WEBHOOK_CLAIM_DUPLICATE,
                resource=audit_resource,
                result="ok",
                dept_id=dept_id,
                payload={
                    "delivery_id": delivery_id,
                    "event_type": event.event_type,
                },
                now=now,
            ),
        )
        return _json_response(
            {"status": "duplicate", "reason": _REASON_WEBHOOK_CLAIM_DUPLICATE},
            status_code=status.HTTP_200_OK,
        )

    workflow_input = _workflow_input_for_event(
        event,
        dept_id=dept_id,
        trace_id=delivery_id,
    )

    try:
        result: StartResult = await start_workflow_idempotent(
            deps.workflow_client,
            _WORKFLOW_NAME,
            workflow_id,
            [workflow_input],
            task_queue=_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001 - we audit then re-raise
        # Release the claim so the webhook provider's retry
        # picks up the same delivery_id and re-enters the chain.
        try:
            await deps.processed_events.release(delivery_id)
        except Exception as release_exc:  # noqa: BLE001 - best-effort
            _LOG.warning(
                "processed_events.release_failed",
                extra={
                    "delivery_id": delivery_id,
                    "error": type(release_exc).__name__,
                },
            )
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                action=_REASON_WORKFLOW_START_FAILED,
                resource=audit_resource,
                result="error",
                dept_id=dept_id,
                payload={
                    "delivery_id": delivery_id,
                    "workflow_id": workflow_id,
                    "event_type": event.event_type,
                    "error": type(exc).__name__,
                },
                now=now,
            ),
        )
        # Re-raise so FastAPI emits 500 — Atlassian's retry policy
        # then re-fires the webhook and the (now-released) claim is
        # re-eligible.
        raise

    duration_ms = int((monotonic_clock() - started_at_monotonic) * 1000)
    audit_action = (
        _REASON_WORKFLOW_ALREADY_STARTED
        if result.was_existing
        else _REASON_WORKFLOW_STARTED
    )
    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            action=audit_action,
            resource=audit_resource,
            result="ok",
            dept_id=dept_id,
            payload={
                "delivery_id": delivery_id,
                "workflow_id": result.execution_id,
                "event_type": event.event_type,
                "filter_reason": decision.reason,
                "was_existing": result.was_existing,
                "duration_ms": duration_ms,
                "coalesced_with": list(decision.coalesced_with),
            },
            now=now,
        ),
    )

    return _json_response(
        {
            "status": "accepted",
            "decision": "accepted",
            "workflow_id": result.execution_id,
            "was_existing": result.was_existing,
            "filter_reason": decision.reason,
        },
        status_code=status.HTTP_202_ACCEPTED,
    )


async def _reject_for_license_cap(
    *,
    deps: WebhooksEndpointDeps,
    event: WebhookEvent,
    workflow_id: str,
    dept_id: str,
    audit_resource: str,
    exc: BotLicenseCapExceededError,
    now: datetime,
) -> JSONResponse:
    """Translate a license-cap breach into HTTP 429 + audit + Jira ack.

    Called from :func:`_dispatch_pass` when
    :attr:`WebhooksEndpointDeps.license_cap_enforcer` raises
    :class:`BotLicenseCapExceededError`. Three side effects fan out
    from this single rejection:

    1. **Audit row** (``webhook_workflow_start_blocked_license_cap``)
       — captures the *delivery* metadata (delivery_id, event_type,
       workflow_id) so operators can correlate the 429 back to the
       Atlassian envelope. The middleware itself emits a separate
       ``bot_license_cap_exceeded`` row carrying the cap-side payload
       (license_id, current_value, max_value); the two together cover
       both halves of the rejection.
    2. **Best-effort Jira comment** — only fires when ``issue_key``
       is set (Bitbucket-only flows leave it ``None``) and the
       ``jira_ack_comment_poster`` callback is wired. The comment
       template is a short Turkish acknowledgement
       stating the cap and the current usage so the human reporter
       knows the bot is throttled rather than ignoring them.
    3. **HTTP 429** — body ``{"error": "bot_license_cap_exceeded",
       "limit": <type>, "current": <int|float>, "max": <int|float>}``
       so retrying clients (or the admin dashboard) can render a
       precise message without parsing the audit log.

    The idempotency claim is **not** taken — see the
    :func:`_dispatch_pass` docstring for the rationale (Atlassian
    redelivers, the next attempt re-enters the chain, and a fresh
    claim lands when capacity frees up).
    """

    # Coerce numeric values to JSON-friendly primitives. ``Decimal``
    # comes out of the monthly-token branch where the cap is a USD
    # ``NUMERIC(10,2)``; the integer caps stay integers. The HTTP
    # body and audit payload carry primitives downstream callers can
    # compare without parsing strings.
    if isinstance(exc.current, Decimal):
        current_json: float | int = float(exc.current)
    else:
        current_json = int(exc.current)
    if isinstance(exc.max, Decimal):
        max_json: float | int = float(exc.max)
    else:
        max_json = int(exc.max)

    response_body: dict[str, Any] = {
        "error": "bot_license_cap_exceeded",
        "limit": exc.limit_type,
        "current": current_json,
        "max": max_json,
    }

    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            action=_REASON_WEBHOOK_BLOCKED_LICENSE_CAP,
            resource=audit_resource,
            result="denied",
            dept_id=dept_id,
            payload={
                "delivery_id": event.delivery_id,
                "event_type": event.event_type,
                "workflow_id": workflow_id,
                "issue_key": event.issue_key,
                "limit_type": exc.limit_type,
                "current_value": current_json,
                "max_value": max_json,
                "license_id": exc.license_id,
            },
            now=now,
        ),
    )

    # Best-effort Jira acknowledgement. Only fires for Jira-driven
    # starts that surfaced an ``issue_key`` and only when the poster
    # is wired. Errors inside the poster are caught here as a second
    # line of defence — the production binding already swallows its
    # own failures, but we double-guard so a misconfigured callback
    # cannot corrupt the rejection signal.
    poster = deps.jira_ack_comment_poster
    if poster is not None and event.issue_key:
        comment_body = (
            "🤖 Bot lisans limiti aşıldı "
            f"({exc.limit_type}: {current_json}/{max_json}). "
            "Mevcut workflow'lar bittiğinde tekrar denenecek."
        )
        try:
            await poster(dept_id, event.issue_key, comment_body)
        except Exception as comment_exc:  # noqa: BLE001 - best-effort
            _LOG.warning(
                "license_cap_ack_comment_failed",
                extra={
                    "issue_key": event.issue_key,
                    "dept_id": dept_id,
                    "error": type(comment_exc).__name__,
                },
            )

    return _json_response(
        response_body,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    )


async def _drop_response(
    *,
    deps: WebhooksEndpointDeps,
    decision: FilterDecision,
    audit_resource: str,
    dept_id: str | None,
    event: WebhookEvent,
    now: datetime,
) -> JSONResponse:
    """Audit a chain ``"drop"`` decision and reply 200.

    The pass-through flavour decisions
    (``streamlit_inline_reply_with_bypass``,
    ``mention_filter_first_iter_exception``, ``filter_chain_pass``)
    are handled by the caller — only genuine drops route through this
    helper. The HTTP body distinguishes coalesced bursts so operators
    can see "this delivery was merged into an open window" without
    diffing audit rows.
    """

    response_body: dict[str, Any] = {
        "status": "dropped",
        "reason": decision.reason,
    }
    if decision.coalesced_with:
        response_body["coalesced_with"] = list(decision.coalesced_with)

    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            action=decision.reason,
            resource=audit_resource,
            result="ok",
            dept_id=dept_id,
            payload={
                "delivery_id": event.delivery_id,
                "event_type": event.event_type,
                "coalesced_with": list(decision.coalesced_with),
            },
            now=now,
        ),
    )

    return _json_response(response_body, status_code=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# POST /webhooks/jira
# ---------------------------------------------------------------------------


@router.post("/jira")
async def post_jira_webhook(request: Request) -> JSONResponse:
    """Handle Jira webhook events."""

    return await _process_webhook(request, provider="jira")


# ---------------------------------------------------------------------------
# POST /webhooks/bitbucket
# ---------------------------------------------------------------------------


@router.post("/bitbucket")
async def post_bitbucket_webhook(request: Request) -> JSONResponse:
    """Handle Bitbucket webhook events."""

    return await _process_webhook(request, provider="bitbucket")


# ---------------------------------------------------------------------------
# Shared dispatch
# ---------------------------------------------------------------------------


async def _process_webhook(  # noqa: PLR0911, PLR0912, PLR0915 - sequential chain
    request: Request, *, provider: Literal["jira", "bitbucket"]
) -> JSONResponse:
    """Sequential dispatch shared by the Jira and Bitbucket endpoints.

    The function intentionally has many branches because each filter
    outcome maps to a distinct HTTP response + audit row pair and the
    only way to keep this readable is one branch per outcome. Splitting
    further (a class-based dispatcher, a state machine) would obscure
    the contract since each step is straight-line execution.
    """

    deps = _build_deps(request)
    if deps is None:
        # Boot-time edge case: the lifespan handler has not finished
        # wiring ``app.state.webhooks`` yet. We surface 503 so the
        # webhook provider's retry will pick the request back up once
        # startup completes.
        return _json_response(
            {
                "status": "service_unavailable",
                "reason": "webhooks_not_wired",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    clock = deps.clock or _utc_now
    monotonic_clock = deps.monotonic_clock or time.monotonic
    started_at_monotonic = monotonic_clock()
    now = clock()

    audit_resource = f"webhook:{provider}"

    # ---- (a) read body / headers -----------------------------------
    raw_body: bytes = await request.body()
    signature = (
        request.headers.get(_HEADER_HMAC_BY_PROVIDER[provider]) or ""
    )
    delivery_id = _resolve_delivery_id(request, provider)

    payload = _safe_json_loads(raw_body)
    if payload is None:
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                action="webhook_bad_request",
                resource=audit_resource,
                result="denied",
                dept_id=None,
                payload={"reason": "invalid_json", "delivery_id": delivery_id},
                now=now,
            ),
        )
        return _json_response(
            {"status": "bad_request", "reason": "invalid_json"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # ---- (b) determine event_type ----------------------------------
    event_type: str
    if provider == "jira":
        body_event = payload.get("webhookEvent")
        event_type = body_event if isinstance(body_event, str) else ""
    else:
        # Bitbucket — the event type lives in the X-Event-Key header.
        event_type = request.headers.get(_HEADER_BITBUCKET_EVENT_KEY) or ""
        # Some test fixtures embed the event type in the body; accept
        # that as a secondary source so the same payload shape works
        # for both production deliveries and curl-based smoke tests.
        if not event_type:
            body_event = payload.get("event") or payload.get("eventKey")
            event_type = body_event if isinstance(body_event, str) else ""

    # ---- Event-type allowlist + loop_guard short-cut ----------------------
    if provider == "jira":
        if event_type not in JIRA_SUPPORTED_EVENTS:
            return await _ignore_unsupported_event(
                deps,
                provider=provider,
                event_type=event_type,
                delivery_id=delivery_id,
                audit_resource=audit_resource,
                now=now,
            )
    else:
        if event_type in BITBUCKET_LOOP_GUARD_EVENTS:
            return await _drop_loop_guarded_bitbucket_event(
                deps,
                event_type=event_type,
                delivery_id=delivery_id,
                audit_resource=audit_resource,
                now=now,
            )
        if event_type not in BITBUCKET_SUPPORTED_EVENTS:
            return await _ignore_unsupported_event(
                deps,
                provider=provider,
                event_type=event_type,
                delivery_id=delivery_id,
                audit_resource=audit_resource,
                now=now,
            )

    # ---- (d) Normalise the event ----------------------------------
    augmented_payload = _augment_payload_with_hmac(
        payload, body=raw_body, signature=signature
    )
    if provider == "jira":
        event = normalize_jira_event(
            raw_payload=augmented_payload,
            delivery_id=delivery_id,
            event_type=event_type,
        )
    else:
        event = normalize_bitbucket_event(
            raw_payload=augmented_payload,
            delivery_id=delivery_id,
            event_type=event_type,
        )

    # ---- (e) Resolve dept_id for audit (best-effort) --------------
    dept_id: str | None = None
    if provider == "jira":
        if event.project_key and deps.jira_dept_resolver is not None:
            try:
                dept_id = await deps.jira_dept_resolver.resolve_jira_dept(
                    event.project_key
                )
            except Exception as exc:  # noqa: BLE001 - audit decoration
                _LOG.warning(
                    "jira_dept_resolver_failed",
                    extra={"error": type(exc).__name__},
                )
                dept_id = None
    else:
        if event.repo_slug and deps.bitbucket_dept_resolver is not None:
            try:
                dept_id = (
                    await deps.bitbucket_dept_resolver.resolve_bitbucket_dept(
                        event.repo_slug
                    )
                )
            except Exception as exc:  # noqa: BLE001 - audit decoration
                _LOG.warning(
                    "bitbucket_dept_resolver_failed",
                    extra={"error": type(exc).__name__},
                )
                dept_id = None

    # ---- (f) Run the filter chain ---------------------------------
    try:
        decision = deps.chain.evaluate(event)
    except WebhookHmacInvalidError as exc:
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                action=exc.reason,
                resource=audit_resource,
                result="denied",
                dept_id=dept_id,
                payload={
                    "delivery_id": delivery_id,
                    "event_type": event_type,
                },
                now=now,
            ),
        )
        return _json_response(
            {"status": "unauthorized", "reason": exc.reason},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    except WebhookDeptUnresolvedError as exc:
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                action=exc.reason,
                resource=audit_resource,
                result="denied",
                dept_id=dept_id,
                payload={
                    "delivery_id": delivery_id,
                    "event_type": event_type,
                    "project_key": event.project_key,
                    "repo_slug": event.repo_slug,
                },
                now=now,
            ),
        )
        return _json_response(
            {"status": "bad_request", "reason": exc.reason},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # ---- (g) Translate the chain decision -------------------------
    if decision.action == "drop":
        return await _drop_response(
            deps=deps,
            decision=decision,
            audit_resource=audit_resource,
            dept_id=dept_id,
            event=event,
            now=now,
        )

    # ---- (h) Pass — derive workflow_id and dispatch ---------------
    workflow_id = _workflow_id_for(event)
    if workflow_id is None:
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                action="webhook_bad_request",
                resource=audit_resource,
                result="denied",
                dept_id=dept_id,
                payload={
                    "reason": "missing_workflow_id_inputs",
                    "delivery_id": delivery_id,
                    "event_type": event_type,
                    "issue_key": event.issue_key,
                    "repo_slug": event.repo_slug,
                    "pr_id": event.pr_id,
                },
                now=now,
            ),
        )
        return _json_response(
            {
                "status": "bad_request",
                "reason": "missing_workflow_id_inputs",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return await _dispatch_pass(
        deps=deps,
        event=event,
        decision=decision,
        workflow_id=workflow_id,
        dept_id=dept_id,
        audit_resource=audit_resource,
        started_at_monotonic=started_at_monotonic,
        monotonic_clock=monotonic_clock,
        now=now,
    )


async def _ignore_unsupported_event(
    deps: WebhooksEndpointDeps,
    *,
    provider: str,
    event_type: str,
    delivery_id: str,
    audit_resource: str,
    now: datetime,
) -> JSONResponse:
    """Audit + 200 for event types outside the Jira / Bitbucket allowlist.

    Implements the "unsupported event types are silently dropped"
    branch. The response carries the
    ``webhook_event_ignored`` reason so callers (test harnesses,
    operators inspecting the proxy) can confirm the URL was reached
    even though the event was discarded.
    """

    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            action=_REASON_WEBHOOK_EVENT_IGNORED,
            resource=audit_resource,
            result="ok",
            dept_id=None,
            payload={
                "delivery_id": delivery_id,
                "event_type": event_type,
                "provider": provider,
            },
            now=now,
        ),
    )
    return _json_response(
        {
            "status": "ignored",
            "reason": _REASON_WEBHOOK_EVENT_IGNORED,
            "event_type": event_type,
        },
        status_code=status.HTTP_200_OK,
    )


async def _drop_loop_guarded_bitbucket_event(
    deps: WebhooksEndpointDeps,
    *,
    event_type: str,
    delivery_id: str,
    audit_resource: str,
    now: datetime,
) -> JSONResponse:
    """Audit + 200 for ``pullrequest:fulfilled`` (loop guard short-cut).

    Bitbucket's merge event is recognised but always dropped:

    * The bot itself never merges; any merge we observe is therefore a human
      action that has already taken effect.
    * Re-entering the filter chain would burn HMAC + dept-resolve
      cycles only to land at the same conclusion (loop_guard).

    We surface the drop as ``loop_guard_dropped`` so the audit row is
    grouped with the bot self-action drops the chain would have
    produced anyway.
    """

    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            action=REASON_LOOP_GUARD_DROPPED,
            resource=audit_resource,
            result="ok",
            dept_id=None,
            payload={
                "delivery_id": delivery_id,
                "event_type": event_type,
                "reason_detail": "bitbucket_pullrequest_fulfilled",
            },
            now=now,
        ),
    )
    return _json_response(
        {
            "status": "dropped",
            "reason": REASON_LOOP_GUARD_DROPPED,
            "event_type": event_type,
        },
        status_code=status.HTTP_200_OK,
    )
