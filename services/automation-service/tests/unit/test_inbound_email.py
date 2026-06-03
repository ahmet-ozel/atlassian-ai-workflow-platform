"""Unit tests for the email-to-task IMAP poller.

The poller's pure helpers (``parse_inbound_email``,
``extract_email_intent_text``, ``EmailInboundConfig``) and its
asynchronous side-effects are exercised via an in-memory IMAP
mailbox fake. Each test walks at most one poll cycle so the
event loop never sleeps.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.inbound.common import InboundContext  # noqa: E402
from automation_service.inbound.email_to_task import (  # noqa: E402
    EmailInboundConfig,
    EmailToTaskPoller,
    config_from_env,
    extract_email_intent_text,
    parse_inbound_email,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _FakeDeptResolver:
    email_map: dict[str, str] = field(default_factory=dict)

    async def resolve_for_slack(
        self, *, team_id: str | None, channel_id: str | None
    ) -> str | None:
        return None

    async def resolve_for_email(self, *, recipient: str) -> str | None:
        return self.email_map.get(recipient.lower())


class _FakeSlackVerifier:
    async def verify(self, **_: Any) -> bool:
        return False  # never used in email tests


@dataclass
class _FakeWorkflowClient:
    calls: list[dict[str, Any]] = field(default_factory=list)
    _started_ids: set[str] = field(default_factory=set)
    raise_already_started: bool = False

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "workflow": workflow,
                "args": list(args),
                "id": id,
                "task_queue": task_queue,
            }
        )
        if self.raise_already_started or id in self._started_ids:
            from temporalio.exceptions import WorkflowAlreadyStartedError

            raise WorkflowAlreadyStartedError(
                workflow_id=id,
                workflow_type=workflow,
                run_id=None,
            )
        self._started_ids.add(id)
        return object()


@dataclass
class _FakeMailbox:
    """In-memory IMAP mailbox returning hand-crafted RFC 5322 bytes."""

    messages: list[tuple[str, bytes]] = field(default_factory=list)
    connect_calls: int = 0
    disconnect_calls: int = 0
    fetch_calls: int = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def fetch_unseen(self) -> AsyncIterator[tuple[str, bytes]]:
        self.fetch_calls += 1
        for uid, body in self.messages:
            yield uid, body
        self.messages = []  # mark as seen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _email_bytes(
    *,
    from_addr: str = "alice@example.com",
    to_addr: str = "bot@example.com",
    subject: str = "please open a ticket",
    body: str = "We need to fix the login bug",
    message_id: str = "<msg-1@example.com>",
) -> bytes:
    return (
        f"From: {from_addr}\r\n"
        f"To: {to_addr}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


def _make_ctx(
    *,
    audit_logger: AuditLogger,
    dept_resolver: _FakeDeptResolver,
    workflow_client: _FakeWorkflowClient,
) -> InboundContext:
    return InboundContext(
        dept_resolver=dept_resolver,
        workflow_client=workflow_client,
        slack_verifier=_FakeSlackVerifier(),
        audit_logger=audit_logger,
        env={},
        now_fn=lambda: _FROZEN_NOW,
    )


def _make_poller(
    *,
    mailbox: _FakeMailbox,
    audit_logger: AuditLogger,
    dept_resolver: _FakeDeptResolver,
    workflow_client: _FakeWorkflowClient,
    inbound_address: str = "bot@example.com",
) -> EmailToTaskPoller:
    config = EmailInboundConfig(
        inbound_address=inbound_address,
        imap_host="imap.example.com",
        imap_username="bot",
    )
    ctx = _make_ctx(
        audit_logger=audit_logger,
        dept_resolver=dept_resolver,
        workflow_client=workflow_client,
    )
    return EmailToTaskPoller(config=config, mailbox=mailbox, ctx=ctx)


# ---------------------------------------------------------------------------
# Pure-helper tests
# ---------------------------------------------------------------------------


class TestExtractEmailIntentText:
    def test_strips_signature_block(self) -> None:
        out = extract_email_intent_text("please help\n-- \nAlice\nCEO")
        assert out == "please help"

    def test_strips_quoted_reply(self) -> None:
        out = extract_email_intent_text(
            "thanks!\nOn Mon, Jun 1, 2024 Alice <a@x> wrote:\n> hi"
        )
        assert out == "thanks!"

    def test_idempotent(self) -> None:
        a = extract_email_intent_text("hello\n-- \nbye")
        b = extract_email_intent_text(a)
        assert a == b

    def test_no_signature_returns_trimmed(self) -> None:
        assert extract_email_intent_text("  hello world  ") == "hello world"

    def test_non_string_raises(self) -> None:
        with pytest.raises(TypeError):
            extract_email_intent_text(b"bytes")  # type: ignore[arg-type]


class TestParseInboundEmail:
    def test_basic_parse(self) -> None:
        parsed = parse_inbound_email(_email_bytes())
        assert parsed is not None
        assert parsed.from_address == "alice@example.com"
        assert "bot@example.com" in parsed.to_addresses
        assert parsed.subject == "please open a ticket"
        assert "fix the login bug" in parsed.body_text
        assert parsed.message_id == "msg-1@example.com"

    def test_missing_from_returns_none(self) -> None:
        raw = (
            b"To: bot@example.com\r\n"
            b"Subject: hi\r\n"
            b"Message-ID: <m@x>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        assert parse_inbound_email(raw) is None

    def test_missing_message_id_falls_back_to_hash(self) -> None:
        raw = (
            b"From: alice@example.com\r\n"
            b"To: bot@example.com\r\n"
            b"Subject: x\r\n"
            b"\r\n"
            b"body"
        )
        parsed = parse_inbound_email(raw)
        assert parsed is not None
        # SHA-256 hex is 64 chars.
        assert len(parsed.message_id) == 64
        assert all(c in "0123456789abcdef" for c in parsed.message_id)

    def test_recipients_lowercased(self) -> None:
        parsed = parse_inbound_email(
            _email_bytes(to_addr="Bot@EXAMPLE.com")
        )
        assert parsed is not None
        assert "bot@example.com" in parsed.to_addresses


# ---------------------------------------------------------------------------
# EmailInboundConfig validation
# ---------------------------------------------------------------------------


class TestEmailInboundConfig:
    def test_invalid_address_rejected(self) -> None:
        with pytest.raises(ValueError):
            EmailInboundConfig(
                inbound_address="not-an-email",
                imap_host="imap.example.com",
                imap_username="bot",
            )

    def test_zero_poll_interval_rejected(self) -> None:
        with pytest.raises(ValueError):
            EmailInboundConfig(
                inbound_address="bot@example.com",
                imap_host="imap.example.com",
                imap_username="bot",
                poll_interval_s=0,
            )

    def test_port_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            EmailInboundConfig(
                inbound_address="bot@example.com",
                imap_host="imap.example.com",
                imap_username="bot",
                imap_port=70000,
            )


class TestConfigFromEnv:
    def test_returns_none_when_address_missing(self) -> None:
        assert config_from_env({}) is None

    def test_raises_when_imap_block_incomplete(self) -> None:
        with pytest.raises(ValueError, match="EMAIL_INBOUND_IMAP_HOST"):
            config_from_env({"EMAIL_INBOUND_ADDRESS": "bot@example.com"})

    def test_builds_config_from_full_env(self) -> None:
        cfg = config_from_env(
            {
                "EMAIL_INBOUND_ADDRESS": "bot@example.com",
                "EMAIL_INBOUND_IMAP_HOST": "imap.example.com",
                "EMAIL_INBOUND_IMAP_USER": "bot",
                "EMAIL_INBOUND_IMAP_PORT": "143",
                "EMAIL_INBOUND_POLL_INTERVAL_S": "10",
                "EMAIL_INBOUND_IMAP_USE_SSL": "false",
            }
        )
        assert cfg is not None
        assert cfg.inbound_address == "bot@example.com"
        assert cfg.imap_port == 143
        assert cfg.poll_interval_s == 10.0
        assert cfg.use_ssl is False


# ---------------------------------------------------------------------------
# Poll-cycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_starts_workflow_for_matching_email() -> None:
    audit_logger = AuditLogger(writer=_RecordingAuditWriter())
    sink: _RecordingAuditWriter = audit_logger._writer  # type: ignore[assignment]
    workflow_client = _FakeWorkflowClient()
    mailbox = _FakeMailbox(messages=[("u1", _email_bytes())])
    poller = _make_poller(
        mailbox=mailbox,
        audit_logger=audit_logger,
        dept_resolver=_FakeDeptResolver(email_map={"bot@example.com": "payment"}),
        workflow_client=workflow_client,
    )

    started = await poller.poll_once()

    assert started == 1
    assert len(workflow_client.calls) == 1
    call = workflow_client.calls[0]
    assert call["workflow"] == "AutomationWorkflow"
    assert call["task_queue"] == "automation-tq"
    wf_input = call["args"][0]
    assert wf_input["channel"] == "email"
    assert wf_input["auto_assign"] is True
    assert wf_input["smart_defaults"] is True
    assert wf_input["department_id"] == "payment"
    assert wf_input["actor_handle"] == "alice@example.com"
    # The intent text comes from the body (signature-stripped).
    assert "fix the login bug" in wf_input["intent_text"]

    actions = [e.action for e in sink.events]
    assert "inbound_workflow_started" in actions


@pytest.mark.asyncio
async def test_poll_once_skips_recipient_mismatch() -> None:
    audit_logger = AuditLogger(writer=_RecordingAuditWriter())
    sink: _RecordingAuditWriter = audit_logger._writer  # type: ignore[assignment]
    workflow_client = _FakeWorkflowClient()
    mailbox = _FakeMailbox(
        messages=[("u1", _email_bytes(to_addr="someone-else@example.com"))]
    )
    poller = _make_poller(
        mailbox=mailbox,
        audit_logger=audit_logger,
        dept_resolver=_FakeDeptResolver(email_map={"bot@example.com": "payment"}),
        workflow_client=workflow_client,
    )

    started = await poller.poll_once()

    assert started == 0
    assert workflow_client.calls == []
    actions = [e.action for e in sink.events]
    assert "inbound_email_recipient_mismatch" in actions


@pytest.mark.asyncio
async def test_poll_once_dept_unresolved_emits_audit_no_workflow() -> None:
    audit_logger = AuditLogger(writer=_RecordingAuditWriter())
    sink: _RecordingAuditWriter = audit_logger._writer  # type: ignore[assignment]
    workflow_client = _FakeWorkflowClient()
    mailbox = _FakeMailbox(messages=[("u1", _email_bytes())])
    # No mapping configured → dept_unresolved.
    poller = _make_poller(
        mailbox=mailbox,
        audit_logger=audit_logger,
        dept_resolver=_FakeDeptResolver(email_map={}),
        workflow_client=workflow_client,
    )

    started = await poller.poll_once()

    assert started == 0
    assert workflow_client.calls == []
    actions = [e.action for e in sink.events]
    assert "inbound_dept_unresolved" in actions


@pytest.mark.asyncio
async def test_poll_once_idempotent_via_already_started() -> None:
    audit_logger = AuditLogger(writer=_RecordingAuditWriter())
    sink: _RecordingAuditWriter = audit_logger._writer  # type: ignore[assignment]
    workflow_client = _FakeWorkflowClient(raise_already_started=True)
    mailbox = _FakeMailbox(messages=[("u1", _email_bytes())])
    poller = _make_poller(
        mailbox=mailbox,
        audit_logger=audit_logger,
        dept_resolver=_FakeDeptResolver(email_map={"bot@example.com": "payment"}),
        workflow_client=workflow_client,
    )

    started = await poller.poll_once()

    # ``was_existing`` is True → ``started`` counter does not increment.
    assert started == 0
    actions = [e.action for e in sink.events]
    assert "inbound_workflow_already_started" in actions


@pytest.mark.asyncio
async def test_poll_once_skips_already_seen_uid() -> None:
    audit_logger = AuditLogger(writer=_RecordingAuditWriter())
    workflow_client = _FakeWorkflowClient()
    mailbox = _FakeMailbox(messages=[("u1", _email_bytes())])
    poller = _make_poller(
        mailbox=mailbox,
        audit_logger=audit_logger,
        dept_resolver=_FakeDeptResolver(email_map={"bot@example.com": "payment"}),
        workflow_client=workflow_client,
    )
    poller._seen_uids.add("u1")

    started = await poller.poll_once()
    assert started == 0
    assert workflow_client.calls == []


@pytest.mark.asyncio
async def test_poll_once_falls_back_to_subject_when_body_empty() -> None:
    audit_logger = AuditLogger(writer=_RecordingAuditWriter())
    workflow_client = _FakeWorkflowClient()
    raw = _email_bytes(subject="please help with login", body="")
    mailbox = _FakeMailbox(messages=[("u1", raw)])
    poller = _make_poller(
        mailbox=mailbox,
        audit_logger=audit_logger,
        dept_resolver=_FakeDeptResolver(email_map={"bot@example.com": "payment"}),
        workflow_client=workflow_client,
    )

    started = await poller.poll_once()
    assert started == 1
    assert (
        workflow_client.calls[0]["args"][0]["intent_text"]
        == "please help with login"
    )
