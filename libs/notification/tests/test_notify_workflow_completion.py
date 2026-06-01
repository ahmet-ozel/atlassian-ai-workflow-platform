"""Unit tests for ``NotificationService.notify_workflow_completion``.

Covers the decision table from
``.kiro/specs/platform-mimari-ops/design.md`` §`NotificationService`
(task 8.2):

* ``status == "failed"`` ⇒ Slack send is **mandatory** regardless of
  ``dept.notify_on_success``; email is sent iff ``notify_email`` is
  set or ``"email"`` ∈ ``notify_channels``.
* ``status ∈ {"completed","partial"}`` and
  ``dept.notify_on_success == False`` ⇒ no-op.
* ``notify_on_success == True`` ⇒ dispatch on the dept's
  ``notify_channels`` set.
* Idempotent retry — the same ``workflow_id`` + channel + kind hashes to
  the same ``dedup_key``; the second attempt skips the adapter send when
  the store reports a duplicate row.
* Body redaction — the persisted ``target`` and ``body_hash`` are
  sha256 digests, never plain webhook URLs / email bodies.

The tests run on lightweight in-memory fakes for the four injected
collaborators so the suite has no network / Postgres dependency. The
fakes are also useful for sibling task 8.6 (Property 18 hypothesis
test) which will reuse them as the SUT.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

import pytest

from notification import (
    DeptConfigView,
    NotificationError,
    NotificationLogEntry,
    NotificationService,
    TemplateRenderError,
    WorkflowResult,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeSlackAdapter:
    """In-memory ``SlackAdapter`` that records ``(body, webhook)`` pairs."""

    sends: list[tuple[str, str]] = field(default_factory=list)
    admin_sends: list[tuple[str, str]] = field(default_factory=list)
    raise_on_send: Exception | None = None
    raise_on_admin_send: Exception | None = None

    async def send(self, body: str, *, webhook: str) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sends.append((body, webhook))

    async def send_admin_channel(self, body: str, *, alert_type: str) -> None:
        if self.raise_on_admin_send is not None:
            raise self.raise_on_admin_send
        self.admin_sends.append((body, alert_type))


@dataclass
class _FakeEmailAdapter:
    """In-memory ``EmailAdapter`` that records ``(body, to)`` pairs."""

    sends: list[tuple[str, str]] = field(default_factory=list)
    raise_on_send: Exception | None = None

    async def send(self, body: str, *, to: str) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sends.append((body, to))


@dataclass
class _FakePromptRenderer:
    """In-memory ``PromptRenderer`` returning template-name-aware bodies."""

    bodies: dict[str, str] = field(
        default_factory=lambda: {
            "notifications/workflow_succeeded": "OK: workflow completed",
            "notifications/workflow_failed": ":x: workflow failed",
        }
    )
    render_calls: list[tuple[str, object]] = field(default_factory=list)

    def render(self, name: str, *, vars: object) -> str:
        self.render_calls.append((name, vars))
        if name not in self.bodies:
            raise KeyError(f"unknown prompt {name!r}")
        return self.bodies[name]


@dataclass
class _FakeNotificationLogStore:
    """In-memory ``NotificationLogStore`` honouring ``UNIQUE(dedup_key)``."""

    rows: list[NotificationLogEntry] = field(default_factory=list)
    seen_dedup_keys: set[str] = field(default_factory=set)

    async def insert(self, entry: NotificationLogEntry) -> bool:
        if entry.dedup_key in self.seen_dedup_keys:
            return False
        self.seen_dedup_keys.add(entry.dedup_key)
        self.rows.append(entry)
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service_with_fakes(
    *,
    slack: _FakeSlackAdapter | None = None,
    email: _FakeEmailAdapter | None = None,
    prompts: _FakePromptRenderer | None = None,
    log_store: _FakeNotificationLogStore | None = None,
) -> tuple[
    NotificationService,
    _FakeSlackAdapter,
    _FakeEmailAdapter,
    _FakePromptRenderer,
    _FakeNotificationLogStore,
]:
    """Build a :class:`NotificationService` wired to fresh in-memory fakes."""

    slack = slack or _FakeSlackAdapter()
    email = email or _FakeEmailAdapter()
    prompts = prompts or _FakePromptRenderer()
    log_store = log_store or _FakeNotificationLogStore()
    service = NotificationService(
        slack=slack,
        email=email,
        prompts=prompts,
        log_store=log_store,
    )
    return service, slack, email, prompts, log_store


def _dept(
    *,
    dept_id: str = "payment",
    notify_on_success: bool = False,
    notify_channels: frozenset[str] = frozenset(),
    slack_webhook: str | None = "https://hooks.slack.com/services/T/B/X",
    notify_email: str | None = "ops@example.com",
) -> DeptConfigView:
    return DeptConfigView(
        dept_id=dept_id,
        notify_on_success=notify_on_success,
        notify_channels=frozenset(notify_channels),  # type: ignore[arg-type]
        slack_webhook=slack_webhook,
        notify_email=notify_email,
    )


def _result(status: str = "completed", *, error: str | None = None) -> WorkflowResult:
    return WorkflowResult(  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        summary="dummy",
        error=error,
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Decision-table tests
# ---------------------------------------------------------------------------


def test_completed_with_notify_on_success_false_is_noop() -> None:
    """R5.2 — success-gated branch: no dispatch when dept opted out."""

    service, slack, email, prompts, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=False, notify_channels=frozenset({"slack", "email"})
    )

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-1-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    # No render, no adapter call, no log row — pure no-op.
    assert prompts.render_calls == []
    assert slack.sends == []
    assert email.sends == []
    assert store.rows == []
    assert outcome.slack_sent is False
    assert outcome.email_sent is False


def test_partial_with_notify_on_success_false_is_noop() -> None:
    """``"partial"`` is treated as success for gating (design §`NotificationService`)."""

    service, slack, email, _, store = _service_with_fakes()
    dept = _dept(notify_on_success=False, notify_channels=frozenset({"slack"}))

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-2-1",
            dept=dept,
            result=_result("partial"),
        )
    )

    assert slack.sends == []
    assert email.sends == []
    assert store.rows == []
    assert outcome.slack_sent is False


def test_completed_with_notify_on_success_true_dispatches_listed_channels() -> None:
    """``notify_on_success=True`` ⇒ send on every channel in ``notify_channels``."""

    service, slack, email, prompts, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack", "email"}),
    )

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-3-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    # Body rendered once via the success template.
    assert prompts.render_calls == [
        ("notifications/workflow_succeeded", None),
    ]
    # Both adapters got the same rendered body.
    assert len(slack.sends) == 1
    assert len(email.sends) == 1
    assert slack.sends[0][0] == "OK: workflow completed"
    assert email.sends[0][0] == "OK: workflow completed"
    # And both rows landed in the log.
    assert {r.channel for r in store.rows} == {"slack", "email"}
    assert all(r.status == "sent" for r in store.rows)
    assert outcome.slack_sent and outcome.email_sent


def test_completed_with_only_slack_in_channels_skips_email() -> None:
    """Only listed channels dispatch on the success path."""

    service, slack, email, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-4-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    assert len(slack.sends) == 1
    assert email.sends == []
    assert {r.channel for r in store.rows} == {"slack"}


def test_failed_dispatches_slack_even_when_notify_on_success_false() -> None:
    """R5.3 — failure-mandatory branch: Slack always fires."""

    service, slack, email, prompts, store = _service_with_fakes()
    # notify_on_success=False AND notify_channels=∅ — dept opted out
    # of every success notification. Failure path must still fire.
    dept = _dept(
        notify_on_success=False,
        notify_channels=frozenset(),
    )

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-5-1",
            dept=dept,
            result=_result("failed", error="boom"),
        )
    )

    # Failure template selected.
    assert prompts.render_calls == [
        ("notifications/workflow_failed", None),
    ]
    # Slack body rendered and sent regardless of dept config.
    assert len(slack.sends) == 1
    assert slack.sends[0][0] == ":x: workflow failed"
    # Log row landed.
    assert len(store.rows) >= 1
    slack_rows = [r for r in store.rows if r.channel == "slack"]
    assert len(slack_rows) == 1
    assert slack_rows[0].status == "sent"
    assert outcome.slack_sent is True


def test_failed_with_email_configured_also_sends_email() -> None:
    """R5.3 nuance — failure path emails when ``notify_email`` is configured."""

    service, _, email, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=False,
        notify_channels=frozenset(),
        notify_email="ops@example.com",
    )

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-6-1",
            dept=dept,
            result=_result("failed", error="kaboom"),
        )
    )

    assert len(email.sends) == 1
    assert email.sends[0][1] == "ops@example.com"
    assert outcome.email_sent is True
    # Email row in log.
    email_rows = [r for r in store.rows if r.channel == "email"]
    assert len(email_rows) == 1


def test_failed_with_no_email_configured_skips_email_channel() -> None:
    """No ``notify_email`` ⇒ failure-mandatory still does not email."""

    service, _, email, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=False,
        notify_channels=frozenset(),
        notify_email=None,
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-7-1",
            dept=dept,
            result=_result("failed"),
        )
    )

    assert email.sends == []
    assert all(r.channel != "email" for r in store.rows)


def test_failed_with_no_slack_webhook_skips_slack_channel() -> None:
    """No dept Slack webhook ⇒ skip Slack (sibling task 8.3 covers admin)."""

    service, slack, _, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=False,
        notify_channels=frozenset(),
        slack_webhook=None,
        notify_email=None,
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-8-1",
            dept=dept,
            result=_result("failed"),
        )
    )

    assert slack.sends == []
    assert store.rows == []  # nothing eligible


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_dedup_key_is_deterministic_per_workflow_channel_kind() -> None:
    """Property 18 (d) — dedup_key is sha256(workflow_id, channel, kind)."""

    service, _, _, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-9-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    expected = hashlib.sha256(
        b"payment-PAY-9-1:slack:workflow_completion"
    ).hexdigest()
    assert [r.dedup_key for r in store.rows] == [expected]


def test_idempotent_retry_skips_second_adapter_send() -> None:
    """Property 18 (d) — second attempt with same dedup_key is a no-op send."""

    service, slack, _, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),
    )

    # First call lands.
    outcome_a = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-10-1",
            dept=dept,
            result=_result("completed"),
        )
    )
    # Second call with the same workflow_id — store returns False, the
    # adapter is NOT called again.
    outcome_b = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-10-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    # Adapter saw exactly one send across two calls.
    assert len(slack.sends) == 1
    # Log table has exactly one row.
    assert len(store.rows) == 1
    # Second outcome reports the dedup skip.
    assert outcome_a.slack_sent is True
    assert outcome_b.slack_sent is False
    assert outcome_b.slack_skipped_dedup is True


# ---------------------------------------------------------------------------
# Body / target redaction
# ---------------------------------------------------------------------------


def test_log_row_stores_hashed_target_not_plain_webhook() -> None:
    """Foundation R7.8 parity — webhook URL never lands in the table."""

    service, _, _, _, store = _service_with_fakes()
    webhook = "https://hooks.slack.com/services/T0/B0/SECRET"
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),
        slack_webhook=webhook,
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-11-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    [row] = store.rows
    # ``target`` is a sha256 hex digest, not the webhook URL.
    assert row.target == hashlib.sha256(webhook.encode("utf-8")).hexdigest()
    assert row.target != webhook
    assert "SECRET" not in row.target


def test_log_row_stores_body_hash_not_body() -> None:
    """``body_hash`` ≠ body; only forensic correlation is preserved."""

    service, _, _, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-12-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    [row] = store.rows
    assert row.body_hash == hashlib.sha256(
        b"OK: workflow completed"
    ).hexdigest()
    # Sanity: hash is not the body.
    assert row.body_hash != "OK: workflow completed"


# ---------------------------------------------------------------------------
# Render failures + transport failures
# ---------------------------------------------------------------------------


def test_render_failure_raises_template_render_error() -> None:
    """Missing prompt template ⇒ ``TemplateRenderError`` (never retryable)."""

    prompts = _FakePromptRenderer(bodies={})  # all templates missing
    service, _, _, _, _ = _service_with_fakes(prompts=prompts)
    dept = _dept(notify_on_success=True, notify_channels=frozenset({"slack"}))

    with pytest.raises(TemplateRenderError):
        _run(
            service.notify_workflow_completion(
                workflow_id="payment-PAY-13-1",
                dept=dept,
                result=_result("completed"),
            )
        )


def test_failure_path_reraises_when_slack_adapter_raises() -> None:
    """Failure-mandatory channel transport error ⇒ raises ``NotificationError``."""

    slack = _FakeSlackAdapter(raise_on_send=RuntimeError("slack 503"))
    service, _, _, _, store = _service_with_fakes(slack=slack)
    dept = _dept(
        notify_on_success=False,
        notify_channels=frozenset(),
        notify_email=None,
    )

    with pytest.raises(NotificationError):
        _run(
            service.notify_workflow_completion(
                workflow_id="payment-PAY-14-1",
                dept=dept,
                result=_result("failed"),
            )
        )

    # The log row landed (insert happened before the adapter call); the
    # store's ``UNIQUE`` constraint will dedup any retry.
    assert len(store.rows) == 1
    assert store.rows[0].channel == "slack"


def test_success_path_swallows_adapter_failure() -> None:
    """Best-effort success path does not surface adapter errors."""

    slack = _FakeSlackAdapter(raise_on_send=RuntimeError("slack 503"))
    service, _, _, _, store = _service_with_fakes(slack=slack)
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),
    )

    # No exception raised even though the adapter failed.
    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-15-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    assert outcome.slack_failed is True
    assert outcome.slack_sent is False
    assert len(store.rows) == 1


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------


def test_template_selection_failure_vs_success() -> None:
    """Failure path renders ``workflow_failed``; success path renders ``workflow_succeeded``."""

    service, _, _, prompts, _ = _service_with_fakes()
    dept_success = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),
    )
    dept_failure = _dept(
        notify_on_success=False,
        notify_channels=frozenset(),
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-16-1",
            dept=dept_success,
            result=_result("completed"),
        )
    )
    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-17-1",
            dept=dept_failure,
            result=_result("failed"),
        )
    )

    rendered_names = [n for n, _ in prompts.render_calls]
    assert rendered_names == [
        "notifications/workflow_succeeded",
        "notifications/workflow_failed",
    ]


def test_dedup_keys_are_distinct_across_channels() -> None:
    """One workflow firing on two channels writes two distinct dedup_keys."""

    service, _, _, _, store = _service_with_fakes()
    dept = _dept(
        notify_on_success=True,
        notify_channels=frozenset({"slack", "email"}),
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-18-1",
            dept=dept,
            result=_result("completed"),
        )
    )

    keys = {r.dedup_key for r in store.rows}
    assert len(keys) == 2  # one per channel
