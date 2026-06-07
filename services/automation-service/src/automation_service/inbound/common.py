"""Shared types and pure helpers for the inbound channel adapters.

This module is the deterministic core of the Slack and email
adapters: it owns the signature verification helpers, the workflow-id
formatter and the ``InboundTaskRequest`` → ``workflow_input`` mapper.
Channel-specific glue (FastAPI routes, IMAP polling) lives in the
sibling modules; everything that is tested with hypothesis or unit
tests belongs here.

Module-level invariants
-----------------------

1. **No I/O** - every public callable is a pure function or a
   structural protocol. Side effects (HTTP, IMAP, audit, Temporal)
   are pushed onto the injected collaborators in
   :class:`InboundContext`.
2. **No secret material in errors** - exception messages and audit
   payloads never contain HMAC secrets, raw signatures, raw email
   bodies or Slack tokens. Slack signatures are short-lived and
   redacted by the ``http_shared`` filter, but the helpers here
   deliberately avoid re-emitting them.
3. **Workflow IDs are deterministic functions of the channel +
   external id** (see :func:`build_inbound_workflow_id`). This makes
   ``start_workflow_idempotent`` collapse retries from the same
   Slack/email source onto a single Temporal execution
   using the same idempotency semantics as the webhook flow.

"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from audit_logger import AuditLogger
from temporal_shared.workflow_registry import task_queue_for
from temporal_shared.start_helper import SupportsStartWorkflow

__all__ = [
    "InboundChannel",
    "InboundContext",
    "InboundDeptResolver",
    "InboundTaskRequest",
    "SlackSignatureVerifier",
    "auto_assign_workflow_input",
    "build_inbound_workflow_id",
    "extract_slack_command_text",
    "verify_slack_signature",
    "utc_now",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Channel discriminator used in workflow ids and audit payloads.
InboundChannel = Literal["slack", "email"]

#: Slack signing prefix. Slack constructs the basestring as
#: ``v0:<request-ts>:<raw-body>`` and signs it with HMAC-SHA256;
#: clients receive ``X-Slack-Signature: v0=<hex>``.
_SLACK_SIG_VERSION: str = "v0"

#: Maximum age (in seconds) of an accepted Slack request timestamp.
#: Slack's documented replay window is 5 minutes; we adopt the same
#: figure so a request whose ``X-Slack-Request-Timestamp`` is older
#: than 300s is rejected as a likely replay.
SLACK_TIMESTAMP_TOLERANCE_S: int = 300

#: Workflow type started for inbound-channel requests. The same
#: ``AutomationWorkflow`` powers the Jira-webhook flow; the
#: channel-specific input flag below makes
#: the workflow call the *task-creator* assistant prompt with
#: ``auto_assign=True`` and ``smart_defaults=True``.
INBOUND_WORKFLOW_NAME: str = "AutomationWorkflow"

#: Temporal task queue used for inbound workflows. Mirrors the registry
#: entry for ``AutomationWorkflow`` so all channel starts land on the
#: same automation worker queue.
INBOUND_TASK_QUEUE: str = task_queue_for(INBOUND_WORKFLOW_NAME)

#: Pre-compiled pattern that strips a leading ``<@U12345>`` Slack
#: user mention. Slack delivers mentions as ``<@USER_ID>`` (and
#: optional ``|name`` suffix) at the start of the message text.
_SLACK_MENTION_RE: re.Pattern[str] = re.compile(r"^\s*<@[^>]+>\s*", re.UNICODE)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InboundTaskRequest:
    """Channel-agnostic intake payload.

    Each adapter (:mod:`slack_to_task`, :mod:`email_to_task`) parses
    the channel-specific event into this shape, then calls
    :func:`auto_assign_workflow_input` to produce the Temporal input.

    Attributes:
        channel: ``"slack"`` or ``"email"`` - emitted into audit
            payloads and workflow inputs so downstream activities
            know which surface produced the request.
        external_id: Stable, channel-provided identifier used to
            derive the workflow id. Slack uses the event ``client_msg_id``
            (or ``ts`` if absent); email uses the RFC 5322 ``Message-ID``.
            Two retries from the same channel/external_id collapse
            onto a single Temporal execution.
        dept_id: Department resolved by :class:`InboundDeptResolver`.
            Empty values are not allowed - the adapter must reject
            requests it cannot map to a department.
        actor_handle: A redacted, channel-specific actor identifier
            (Slack ``user_id``, email ``From:`` local-part). Used in
            audit payloads only; never used to gate the workflow.
        intent_text: The user's free-form request, with channel
            decoration (mention prefix, email signatures) stripped.
            This becomes the assistant prompt input downstream.
        title_hint: Optional short label (Slack message permalink
            fragment, email ``Subject``). Forwarded to the workflow
            so the assistant can use it when creating the Jira issue
            summary.
    """

    channel: InboundChannel
    external_id: str
    dept_id: str
    actor_handle: str
    intent_text: str
    title_hint: str | None = None

    def __post_init__(self) -> None:
        # Defensive structural validation - adapters should already
        # have produced a well-formed value; rejecting bad shapes
        # here makes the failure mode obvious during integration.
        if self.channel not in ("slack", "email"):
            raise ValueError(
                f"InboundTaskRequest.channel must be 'slack' or 'email'; "
                f"got {self.channel!r}"
            )
        if not self.external_id or not self.external_id.strip():
            raise ValueError("InboundTaskRequest.external_id must be non-empty")
        if not self.dept_id or not self.dept_id.strip():
            raise ValueError("InboundTaskRequest.dept_id must be non-empty")
        if not self.actor_handle or not self.actor_handle.strip():
            raise ValueError("InboundTaskRequest.actor_handle must be non-empty")
        if not isinstance(self.intent_text, str):
            raise ValueError("InboundTaskRequest.intent_text must be a string")


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class InboundDeptResolver(Protocol):
    """Map an incoming channel signal to a department id.

    Slack-specific resolvers consult ``team_id`` (or the configured
    Slack workspace → dept mapping); email-specific resolvers parse
    the recipient address (``To:`` / ``Delivered-To:``). The
    resolver is intentionally narrow so tests can inject a dict-backed
    fake.
    """

    async def resolve_for_slack(
        self, *, team_id: str | None, channel_id: str | None
    ) -> str | None:
        """Return the dept id for a Slack ``team_id``/``channel_id`` pair."""
        ...

    async def resolve_for_email(self, *, recipient: str) -> str | None:
        """Return the dept id whose inbox is *recipient* (an RFC 5322 address)."""
        ...


@runtime_checkable
class SlackSignatureVerifier(Protocol):
    """Resolve and verify the Slack signing secret for a request.

    Production wires this to :func:`vault_client.read` against
    ``vault:notifications/slack_inbound/<dept_id>``. Tests inject a
    callable returning a static secret so the HMAC chain can be
    exercised end-to-end without Vault.
    """

    async def verify(
        self,
        *,
        dept_id: str | None,
        timestamp: str,
        raw_body: bytes,
        signature: str,
        now: datetime,
    ) -> bool:
        """Return ``True`` iff the Slack signature is valid and fresh."""
        ...


# ---------------------------------------------------------------------------
# InboundContext - bag of dependencies populated from app.state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InboundContext:
    """Runtime collaborators required by the inbound adapters.

    Pulled from ``app.state.inbound`` by the Slack route at request
    time (see :mod:`slack_to_task`). The email poller is constructed
    once at startup with its own copy of the same context.
    """

    dept_resolver: InboundDeptResolver
    workflow_client: SupportsStartWorkflow
    slack_verifier: SlackSignatureVerifier
    audit_logger: AuditLogger
    env: Mapping[str, str]
    now_fn: Callable[[], datetime]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current UTC time (timezone-aware).

    Exposed so callers populate :attr:`InboundContext.now_fn` from a
    single canonical source. The Slack signature verifier and the
    audit-event timestamp share this clock so a frozen-time test
    fixture stays consistent across both surfaces.
    """

    return datetime.now(timezone.utc)


def build_inbound_workflow_id(channel: InboundChannel, external_id: str) -> str:
    """Return the Temporal workflow id for *(channel, external_id)*.

    The format mirrors :mod:`temporal_shared.identifiers` for the
    Jira webhook handler (``automation-jira-<KEY>``) but discriminates
    on ``channel`` so a Slack message and an email with the same
    external id never collide. The ``external_id`` is normalised to
    lowercase + non-alphanumeric characters reduced to ``-`` so the
    resulting id is safe for Temporal (which forbids whitespace and
    a small set of control characters).

    Determinism - the same ``(channel, external_id)`` always yields
    the same workflow id, so retries collapse onto a single
    execution under :func:`start_workflow_idempotent`.

    >>> build_inbound_workflow_id("slack", "1700000000.000123")
    'automation-inbound-slack-1700000000-000123'
    >>> build_inbound_workflow_id("email", "<abc@example.com>")
    'automation-inbound-email-abc-example-com'
    """

    if channel not in ("slack", "email"):
        raise ValueError(
            f"channel must be 'slack' or 'email'; got {channel!r}"
        )
    if not external_id or not external_id.strip():
        raise ValueError("external_id must be a non-empty string")

    # Lowercase + collapse non-alphanumerics to single dashes; trim
    # leading/trailing dashes so the id never starts or ends with a
    # separator.
    normalised = re.sub(r"[^A-Za-z0-9]+", "-", external_id).strip("-").lower()
    if not normalised:
        # All characters were replaced - fall back to a hash so the id
        # remains deterministic and non-empty.
        normalised = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:16]
    return f"automation-inbound-{channel}-{normalised}"


def auto_assign_workflow_input(req: InboundTaskRequest) -> dict[str, Any]:
    """Build the Temporal workflow input for an inbound task request.

    The shape mirrors the Jira-webhook ``workflow_input`` (so the
    ``AutomationWorkflow`` accepts it) but flips two flags that mark
    the request as coming through the *standard task-creator path*:

    * ``auto_assign=True`` - Y1: the workflow assigns the resulting
      Jira issue to the bot account automatically (the user did not
      go through the Streamlit Task Creator manually).
    * ``smart_defaults=True`` - Y10: the workflow runs the
      single-question task-creation prompt and fills in any missing
      fields from the dept defaults instead of asking the user
      multiple clarifying questions.

    The ``trigger`` field is set to the channel name so downstream
    activities can distinguish Slack/email-originated tasks from
    Jira-webhook-originated ones (eg. notification templates may
    differ).

    The function is pure and does not depend on the request clock -
    callers that need a timestamp should populate it from
    :attr:`InboundContext.now_fn`.
    """

    payload: dict[str, Any] = {
        "trigger": f"inbound_{req.channel}",
        "channel": req.channel,
        "external_id": req.external_id,
        "department_id": req.dept_id,
        "actor_handle": req.actor_handle,
        "intent_text": req.intent_text,
        "auto_assign": True,
        "smart_defaults": True,
    }
    if req.title_hint is not None:
        payload["title_hint"] = req.title_hint
    return payload


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------


def verify_slack_signature(
    *,
    secret: bytes,
    timestamp: str,
    raw_body: bytes,
    signature: str,
    now: datetime,
    tolerance_s: int = SLACK_TIMESTAMP_TOLERANCE_S,
) -> bool:
    """Return ``True`` iff *signature* matches Slack's HMAC contract.

    Slack constructs the signing basestring as
    ``v0:<X-Slack-Request-Timestamp>:<raw-body>`` and signs it with
    HMAC-SHA256 keyed on the Slack signing secret; the request header
    ``X-Slack-Signature`` carries ``v0=<hex>``. This helper:

    1. Validates that *timestamp* is within ``tolerance_s`` of *now*.
    2. Computes the expected HMAC and compares it in constant time
       with the supplied *signature* (after stripping the ``v0=``
       prefix).

    The function returns ``False`` for any structural problem
    (missing/invalid timestamp, malformed signature header, secret
    of zero length) instead of raising - callers always emit the
    same audit event (``inbound_slack_hmac_failed``) regardless of
    failure cause, so distinguishing the reasons here would only
    leak information about the verification chain.

    Parameters
    ----------
    secret:
        Slack signing secret. Must be the ``bytes`` form (UTF-8); the
        Vault adapter is responsible for encoding.
    timestamp:
        Value of the ``X-Slack-Request-Timestamp`` header - a
        Unix-epoch integer as ASCII.
    raw_body:
        The exact request body as received over the wire (no
        re-encoding). Slack's signature is byte-for-byte sensitive.
    signature:
        Value of the ``X-Slack-Signature`` header (eg. ``v0=abc...``).
    now:
        The current time, supplied by the caller (so tests can
        freeze the clock).
    tolerance_s:
        Maximum allowed delta in seconds between ``timestamp`` and
        ``now``. Defaults to :data:`SLACK_TIMESTAMP_TOLERANCE_S`.
    """

    if not secret or not timestamp or not signature:
        return False
    if not signature.startswith(f"{_SLACK_SIG_VERSION}="):
        return False

    # Timestamp freshness
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    delta = abs(int(now.timestamp()) - ts_int)
    if delta > tolerance_s:
        return False

    # HMAC compare
    basestring = f"{_SLACK_SIG_VERSION}:{timestamp}:".encode("utf-8") + raw_body
    expected = hmac.new(secret, basestring, hashlib.sha256).hexdigest()
    expected_header = f"{_SLACK_SIG_VERSION}={expected}"
    return hmac.compare_digest(expected_header, signature)


# ---------------------------------------------------------------------------
# Slack mention parsing (pure)
# ---------------------------------------------------------------------------


def extract_slack_command_text(raw_text: str) -> str:
    """Strip the leading ``<@USER>`` mention from a Slack message.

    Slack delivers app mentions as ``<@U12345> please open a ticket``
    or ``<@U12345|displayname> ...`` at the start of the text. This
    helper removes that prefix and surrounding whitespace so the
    downstream task-creator prompt sees the user's actual request.

    The function is whitespace-tolerant and idempotent - passing an
    already-stripped message returns it unchanged.

    >>> extract_slack_command_text("<@U07ABCDEF> open a ticket for the API")
    'open a ticket for the API'
    >>> extract_slack_command_text("<@U07ABCDEF|alice>  fix login bug ")
    'fix login bug'
    >>> extract_slack_command_text("just a plain message")
    'just a plain message'
    """

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    stripped = _SLACK_MENTION_RE.sub("", raw_text, count=1)
    return stripped.strip()
