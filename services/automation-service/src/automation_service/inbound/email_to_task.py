"""Email → Jira task adapter.

Polls an IMAP mailbox at a fixed cadence and creates a Jira task per
accepted message. Configured by the ``EMAIL_INBOUND_ADDRESS`` env var
plus the IMAP connection block (``EMAIL_INBOUND_IMAP_HOST``,
``EMAIL_INBOUND_IMAP_USER``, ``EMAIL_INBOUND_IMAP_PASSWORD_REF``).

Like the Slack adapter, the poller never calls the Atlassian MCP
directly — it forwards parsed messages to ``AutomationWorkflow`` via
:func:`start_workflow_idempotent` so the audit / capability / loop
guard chain stays consistent with the Jira webhook flow.

Architecture
------------

The poller is a *transport* — it knows how to fetch messages from
IMAP and how to translate them into :class:`InboundTaskRequest`
values; it does not know how to talk to Vault, Temporal or audit. All
of those collaborators are injected through :class:`InboundContext`,
matching the pattern used by :mod:`slack_to_task`. This makes the
poller easy to unit-test (the IMAP client is a structural
:class:`ImapMailbox` protocol) and it lets the same adapter run
either as a sidecar background task on the automation-service or as
its own dedicated worker.

Idempotency
-----------

Workflow ids are derived from the RFC 5322 ``Message-ID`` header (or
a SHA-256 of the raw envelope when ``Message-ID`` is missing). A
re-poll over the same mailbox folder produces the same workflow id,
so :func:`start_workflow_idempotent` collapses duplicates. The poller
also keeps an in-memory ``frozenset`` of recently seen UIDs so the
typical "fetch + start" path is a no-op for already-processed
messages.

"""

from __future__ import annotations

import asyncio
import email
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from email.utils import getaddresses, parseaddr
from typing import (
    Any,
    AsyncIterator,
    Iterable,
    Protocol,
    runtime_checkable,
)

from audit_logger import AuditEvent, AuditLogger
from temporal_shared.start_helper import (
    StartResult,
    start_workflow_idempotent,
)

from .common import (
    INBOUND_TASK_QUEUE,
    INBOUND_WORKFLOW_NAME,
    InboundContext,
    InboundTaskRequest,
    auto_assign_workflow_input,
    build_inbound_workflow_id,
)

__all__ = [
    "EmailInboundConfig",
    "EmailToTaskPoller",
    "ImapMailbox",
    "ParsedInboundEmail",
    "extract_email_intent_text",
    "parse_inbound_email",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Audit ``actor_id`` for inbound-email-emitted events.
_AUDIT_ACTOR_ID: str = "automation-service.inbound.email"

#: Audit resource discriminator.
_AUDIT_RESOURCE: str = "webhook:inbound/email"

#: Default IMAP poll cadence (seconds). Overridable via
#: ``EMAIL_INBOUND_POLL_INTERVAL_S`` in the service env.
_DEFAULT_POLL_INTERVAL_S: float = 60.0

#: Default IMAP folder.
_DEFAULT_IMAP_FOLDER: str = "INBOX"

#: Hard cap on the size of the seen-uid set so an unbounded mailbox
#: never causes the poller to hold the entire history in memory. The
#: workflow-id idempotency layer still catches duplicates beyond this
#: window (Temporal's ``WorkflowAlreadyStartedError``).
_SEEN_UIDS_MAX: int = 4096

#: Regex for stripping common email signatures and forwarded-message
#: prefixes from the body before forwarding the intent text.
_SIGNATURE_RE: re.Pattern[str] = re.compile(
    r"\n--\s*\n.*\Z|\nOn\s.*wrote:\s*\n.*\Z",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailInboundConfig:
    """Static configuration for the email poller.

    Attributes:
        inbound_address: The recipient address that triggers task
            creation. Messages whose ``To`` / ``Cc`` / ``Delivered-To``
            do not include this address are skipped (``recipient_mismatch``).
        imap_host: IMAP server hostname.
        imap_port: IMAP server port (default 993 = IMAPS).
        imap_username: IMAP login user.
        imap_folder: Folder to poll. Defaults to ``"INBOX"``.
        poll_interval_s: Cadence between polls in seconds. Must be
            positive — the poller treats zero or negatives as a
            configuration error.
        use_ssl: Whether to use IMAPS. Defaults to ``True``; the
            ``False`` branch exists for the local dev mailcatcher.
    """

    inbound_address: str
    imap_host: str
    imap_username: str
    imap_port: int = 993
    imap_folder: str = _DEFAULT_IMAP_FOLDER
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S
    use_ssl: bool = True

    def __post_init__(self) -> None:
        if not self.inbound_address or "@" not in self.inbound_address:
            raise ValueError(
                "EmailInboundConfig.inbound_address must be a valid email address"
            )
        if not self.imap_host:
            raise ValueError("EmailInboundConfig.imap_host must be non-empty")
        if not self.imap_username:
            raise ValueError("EmailInboundConfig.imap_username must be non-empty")
        if self.imap_port <= 0 or self.imap_port > 65535:
            raise ValueError(
                f"EmailInboundConfig.imap_port out of range: {self.imap_port}"
            )
        if self.poll_interval_s <= 0:
            raise ValueError(
                f"EmailInboundConfig.poll_interval_s must be positive: "
                f"{self.poll_interval_s}"
            )


@dataclass(frozen=True, slots=True)
class ParsedInboundEmail:
    """Channel-agnostic projection of an inbound email message.

    Produced by :func:`parse_inbound_email`; consumed by the poller
    when constructing the :class:`InboundTaskRequest`. Kept separate
    from the request type so the parser stays testable in isolation.
    """

    message_id: str
    from_address: str
    to_addresses: tuple[str, ...]
    subject: str
    body_text: str


# ---------------------------------------------------------------------------
# IMAP mailbox protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ImapMailbox(Protocol):
    """Minimal structural slice of an async IMAP client.

    Production wires this to an ``aioimaplib``-backed adapter. Tests
    inject an in-memory fake yielding fixture messages. The protocol
    is intentionally narrow — only the operations the poller needs
    are part of the contract.
    """

    async def connect(self) -> None:
        """Establish the IMAP connection and select the configured folder."""
        ...

    async def disconnect(self) -> None:
        """Close the IMAP connection (best-effort)."""
        ...

    async def fetch_unseen(self) -> AsyncIterator[tuple[str, bytes]]:
        """Yield ``(uid, raw_rfc822_bytes)`` pairs for unread messages.

        After a message is yielded the implementation should mark it
        as ``\\Seen`` so a future poll does not re-deliver it. The
        poller still tracks UIDs in memory as a belt-and-braces
        guard against IMAP servers that fail to honour the ``\\Seen``
        flag promptly.
        """
        # ``yield`` to make the body an async generator at type-check
        # time. Implementations override this method.
        if False:  # pragma: no cover - protocol body
            yield "", b""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _decode_part(part: Message) -> str:
    """Decode a ``text/plain`` MIME part to ``str``.

    Falls back to UTF-8 with replacement on decoder errors so a single
    malformed body does not crash the poller.
    """

    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _extract_text_body(msg: Message) -> str:
    """Return the message's plain-text body, ignoring HTML alternatives.

    Scans MIME parts in document order and returns the first
    ``text/plain`` payload that is not an attachment. Multipart
    messages without any plain alternative fall back to the empty
    string — the poller still creates the task using the subject as
    the title hint.
    """

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/plain" and "attachment" not in disp:
                return _decode_part(part)
        return ""
    if msg.get_content_type() == "text/plain":
        return _decode_part(msg)
    return ""


def extract_email_intent_text(body_text: str) -> str:
    """Strip signatures and forwarded-message blocks from *body_text*.

    The helper removes everything from the first signature delimiter
    (``\\n-- \\n``) or a ``On ... wrote:`` quote prefix to the end of
    the message. Trailing whitespace is trimmed. The function is
    idempotent and pure.

    >>> extract_email_intent_text("please open a ticket\\n-- \\nAlice")
    'please open a ticket'
    """

    if not isinstance(body_text, str):
        raise TypeError("body_text must be a string")
    cleaned = _SIGNATURE_RE.sub("", body_text)
    return cleaned.strip()


def _normalise_message_id(raw: str | None, fallback_seed: bytes) -> str:
    """Return a stable ``Message-ID`` for workflow-id derivation.

    Uses the supplied ``Message-ID`` after stripping the optional
    ``<...>`` envelope; falls back to a SHA-256 hash of *fallback_seed*
    when the header is missing or empty. The hash fallback is
    deterministic so a re-poll of the same envelope still collapses
    onto a single workflow.
    """

    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate.startswith("<") and candidate.endswith(">"):
            candidate = candidate[1:-1]
        if candidate:
            return candidate
    return hashlib.sha256(fallback_seed).hexdigest()


def parse_inbound_email(
    raw_rfc822: bytes,
) -> ParsedInboundEmail | None:
    """Parse an RFC 5322 byte string into a :class:`ParsedInboundEmail`.

    Returns ``None`` for messages we cannot decode at all (malformed
    envelope). The caller logs an audit event and continues to the
    next message — a single bad email must never wedge the poll loop.
    """

    try:
        msg = email.message_from_bytes(raw_rfc822)
    except Exception:  # noqa: BLE001 - parser hardening
        return None

    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    if not from_addr:
        return None

    # Combine ``To``, ``Cc`` and ``Delivered-To`` (the latter is what
    # MX-routed mail typically carries when the original recipient
    # was a list / alias). Lower-cased for comparison stability.
    raw_recipients: list[str] = []
    for header in ("To", "Cc", "Delivered-To"):
        for value in msg.get_all(header) or []:
            raw_recipients.append(value)
    to_addresses = tuple(
        addr.lower()
        for _, addr in getaddresses(raw_recipients)
        if addr
    )

    subject = msg.get("Subject", "") or ""
    if not isinstance(subject, str):
        subject = str(subject)

    body_text = _extract_text_body(msg)

    message_id = _normalise_message_id(msg.get("Message-ID"), raw_rfc822)
    return ParsedInboundEmail(
        message_id=message_id,
        from_address=from_addr,
        to_addresses=to_addresses,
        subject=subject.strip(),
        body_text=body_text,
    )


def _matches_inbound_address(
    parsed: ParsedInboundEmail, inbound_address: str
) -> bool:
    """Return ``True`` if any recipient header matches *inbound_address*."""

    target = inbound_address.lower()
    return any(addr == target for addr in parsed.to_addresses)


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _make_audit_event(
    *,
    action: str,
    result: str,
    dept_id: str | None,
    payload: dict[str, Any] | None,
    ctx: InboundContext,
) -> AuditEvent:
    return AuditEvent(
        actor_id=_AUDIT_ACTOR_ID,
        actor_role="system",
        dept_id=dept_id,
        action=action,
        resource=_AUDIT_RESOURCE,
        result=result,  # type: ignore[arg-type]
        timestamp=ctx.now_fn(),
        payload=payload,
    )


async def _emit_audit(audit_logger: AuditLogger, event: AuditEvent) -> None:
    try:
        await audit_logger.write(event)
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning(
            "audit_write_failed",
            extra={
                "action": event.action,
                "dept_id": event.dept_id,
                "error": type(exc).__name__,
            },
        )


# ---------------------------------------------------------------------------
# EmailToTaskPoller
# ---------------------------------------------------------------------------


@dataclass
class EmailToTaskPoller:
    """Background poller that turns inbound emails into Jira tasks.

    Lifecycle::

        poller = EmailToTaskPoller(
            config=EmailInboundConfig(...),
            mailbox=ImapMailboxImpl(...),
            ctx=inbound_ctx,
        )
        task = asyncio.create_task(poller.run())
        ...
        await poller.stop()
        await task

    The poller runs until :meth:`stop` is called or
    :attr:`stop_event` is externally set. Errors during a single poll
    cycle are logged + audited but never propagate so the loop survives
    transient IMAP failures.
    """

    config: EmailInboundConfig
    mailbox: ImapMailbox
    ctx: InboundContext
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _seen_uids: set[str] = field(default_factory=set)

    @property
    def stop_event(self) -> asyncio.Event:
        """The :class:`asyncio.Event` that ends the poll loop when set."""

        return self._stop_event

    async def stop(self) -> None:
        """Signal the poll loop to exit at the next iteration."""

        self._stop_event.set()

    async def run(self) -> None:
        """Run the poll loop until :meth:`stop` is called.

        The loop is best-effort — failures inside :meth:`poll_once`
        are caught and logged so the daemon does not die on a single
        IMAP hiccup. The cadence is :attr:`EmailInboundConfig.poll_interval_s`.
        """

        try:
            await self.mailbox.connect()
        except Exception as exc:  # noqa: BLE001 - boot path
            await _emit_audit(
                self.ctx.audit_logger,
                _make_audit_event(
                    action="inbound_email_connect_failed",
                    result="error",
                    dept_id=None,
                    payload={"error": type(exc).__name__},
                    ctx=self.ctx,
                ),
            )
            return

        try:
            while not self._stop_event.is_set():
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001 - loop hardening
                    logger.warning(
                        "inbound_email_poll_failed",
                        extra={"error": type(exc).__name__},
                    )
                    await _emit_audit(
                        self.ctx.audit_logger,
                        _make_audit_event(
                            action="inbound_email_poll_failed",
                            result="error",
                            dept_id=None,
                            payload={"error": type(exc).__name__},
                            ctx=self.ctx,
                        ),
                    )
                # Sleep with cancel-aware wait so :meth:`stop` exits
                # promptly even mid-cadence.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.config.poll_interval_s,
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            try:
                await self.mailbox.disconnect()
            except Exception:  # noqa: BLE001 - best-effort
                pass

    async def poll_once(self) -> int:
        """Process all unseen messages in the configured folder.

        Returns the number of workflows actually started during this
        cycle (recipient mismatches, unparsable bodies and duplicates
        do not count). Useful for tests that want to assert the
        side-effect cardinality of a single iteration.
        """

        started = 0
        async for uid, raw_bytes in self.mailbox.fetch_unseen():
            if uid in self._seen_uids:
                continue
            self._remember_uid(uid)
            handled = await self._process_message(uid, raw_bytes)
            if handled:
                started += 1
        return started

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _remember_uid(self, uid: str) -> None:
        """Record *uid* in the in-memory dedup set with a soft cap."""

        if len(self._seen_uids) >= _SEEN_UIDS_MAX:
            # Drop a stable subset (the iteration order of a set is
            # implementation-defined but deterministic for a given
            # process). The workflow-id idempotency layer still
            # catches anything we evict prematurely.
            evict = next(iter(self._seen_uids))
            self._seen_uids.discard(evict)
        self._seen_uids.add(uid)

    async def _process_message(
        self, uid: str, raw_bytes: bytes
    ) -> bool:
        """Process a single message; return ``True`` if a workflow started."""

        parsed = parse_inbound_email(raw_bytes)
        if parsed is None:
            await _emit_audit(
                self.ctx.audit_logger,
                _make_audit_event(
                    action="inbound_email_parse_failed",
                    result="error",
                    dept_id=None,
                    payload={"uid": uid},
                    ctx=self.ctx,
                ),
            )
            return False

        if not _matches_inbound_address(parsed, self.config.inbound_address):
            await _emit_audit(
                self.ctx.audit_logger,
                _make_audit_event(
                    action="inbound_email_recipient_mismatch",
                    result="ok",
                    dept_id=None,
                    payload={
                        "channel": "email",
                        "uid": uid,
                        "message_id": parsed.message_id,
                        "expected": self.config.inbound_address.lower(),
                    },
                    ctx=self.ctx,
                ),
            )
            return False

        dept_id = await self.ctx.dept_resolver.resolve_for_email(
            recipient=self.config.inbound_address
        )
        if dept_id is None:
            await _emit_audit(
                self.ctx.audit_logger,
                _make_audit_event(
                    action="inbound_dept_unresolved",
                    result="denied",
                    dept_id=None,
                    payload={
                        "channel": "email",
                        "recipient": self.config.inbound_address.lower(),
                        "message_id": parsed.message_id,
                    },
                    ctx=self.ctx,
                ),
            )
            return False

        intent_text = extract_email_intent_text(parsed.body_text)
        # If the body is empty after stripping, fall back to the
        # subject so the workflow still has something to act on.
        if not intent_text:
            intent_text = parsed.subject
        if not intent_text:
            await _emit_audit(
                self.ctx.audit_logger,
                _make_audit_event(
                    action="inbound_email_empty_body",
                    result="ok",
                    dept_id=dept_id,
                    payload={
                        "channel": "email",
                        "message_id": parsed.message_id,
                    },
                    ctx=self.ctx,
                ),
            )
            return False

        try:
            req = InboundTaskRequest(
                channel="email",
                external_id=parsed.message_id,
                dept_id=dept_id,
                actor_handle=parsed.from_address,
                intent_text=intent_text,
                title_hint=parsed.subject or None,
            )
        except ValueError as exc:
            await _emit_audit(
                self.ctx.audit_logger,
                _make_audit_event(
                    action="inbound_bad_request",
                    result="denied",
                    dept_id=dept_id,
                    payload={
                        "channel": "email",
                        "reason": "request_validation_failed",
                        "error": str(exc),
                    },
                    ctx=self.ctx,
                ),
            )
            return False

        workflow_id = build_inbound_workflow_id(req.channel, req.external_id)
        workflow_input = auto_assign_workflow_input(req)

        try:
            result: StartResult = await start_workflow_idempotent(
                self.ctx.workflow_client,
                INBOUND_WORKFLOW_NAME,
                workflow_id,
                [workflow_input],
                task_queue=INBOUND_TASK_QUEUE,
            )
        except Exception as exc:  # noqa: BLE001
            await _emit_audit(
                self.ctx.audit_logger,
                _make_audit_event(
                    action="inbound_workflow_start_failed",
                    result="error",
                    dept_id=dept_id,
                    payload={
                        "channel": "email",
                        "external_id": req.external_id,
                        "workflow_id": workflow_id,
                        "error": type(exc).__name__,
                    },
                    ctx=self.ctx,
                ),
            )
            return False

        audit_action = (
            "inbound_workflow_already_started"
            if result.was_existing
            else "inbound_workflow_started"
        )
        await _emit_audit(
            self.ctx.audit_logger,
            _make_audit_event(
                action=audit_action,
                result="ok",
                dept_id=dept_id,
                payload={
                    "channel": "email",
                    "external_id": req.external_id,
                    "workflow_id": result.execution_id,
                    "was_existing": result.was_existing,
                    "actor_handle": req.actor_handle,
                    "auto_assign": True,
                    "smart_defaults": True,
                },
                ctx=self.ctx,
            ),
        )
        return not result.was_existing


# ---------------------------------------------------------------------------
# Convenience: build poller from env-style config
# ---------------------------------------------------------------------------


def config_from_env(env: dict[str, str]) -> EmailInboundConfig | None:
    """Build an :class:`EmailInboundConfig` from a dict-style env mapping.

    Returns ``None`` when ``EMAIL_INBOUND_ADDRESS`` is unset — the
    poller stays disabled in dev / standalone modes that do not need
    inbound email. Raises :class:`ValueError` when the address is set
    but other required keys are missing.
    """

    address = env.get("EMAIL_INBOUND_ADDRESS")
    if not address:
        return None

    host = env.get("EMAIL_INBOUND_IMAP_HOST")
    user = env.get("EMAIL_INBOUND_IMAP_USER")
    if not host or not user:
        raise ValueError(
            "EMAIL_INBOUND_ADDRESS is set but EMAIL_INBOUND_IMAP_HOST / "
            "EMAIL_INBOUND_IMAP_USER are missing — inbound email cannot "
            "be configured without the IMAP transport."
        )

    port_str = env.get("EMAIL_INBOUND_IMAP_PORT", "993")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(
            f"EMAIL_INBOUND_IMAP_PORT must be an integer, got {port_str!r}"
        ) from exc

    interval_str = env.get(
        "EMAIL_INBOUND_POLL_INTERVAL_S", str(_DEFAULT_POLL_INTERVAL_S)
    )
    try:
        interval = float(interval_str)
    except ValueError as exc:
        raise ValueError(
            f"EMAIL_INBOUND_POLL_INTERVAL_S must be a number, "
            f"got {interval_str!r}"
        ) from exc

    folder = env.get("EMAIL_INBOUND_IMAP_FOLDER", _DEFAULT_IMAP_FOLDER)
    use_ssl = env.get("EMAIL_INBOUND_IMAP_USE_SSL", "true").lower() not in (
        "0",
        "false",
        "no",
    )

    return EmailInboundConfig(
        inbound_address=address,
        imap_host=host,
        imap_username=user,
        imap_port=port,
        imap_folder=folder,
        poll_interval_s=interval,
        use_ssl=use_ssl,
    )


# Re-exports so the ``Iterable`` and ``datetime`` imports remain
# referenced (suppresses unused-import linters in some setups while
# keeping the ``datetime`` available for downstream subclasses).
_unused: tuple[type, ...] = (Iterable, datetime)
