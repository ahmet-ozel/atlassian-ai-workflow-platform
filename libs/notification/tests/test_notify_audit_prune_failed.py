"""Unit tests for ``NotificationService.notify_audit_prune_failed`` (task 8.3).

Covers the contract pinned by:

* design.md §`NotificationService` pseudocode
  (``slack.send_admin_channel(body, alert_type="audit_prune_failed")``).
* design.md §`Property 10` invariant (d) — the admin alarm is mandatory
  and re-uses the dedup_key + ``shared.notification_log`` machinery.
* requirements.md R6.4 — "WHEN AuditPruneWorkflow fail olursa, THE
  Notification_Service SHALL admin'e zorunlu Slack alarmı gönderir;
  ``alert_type: audit_prune_failed``".

The tests run on the same in-memory fakes the workflow-completion suite
uses (``_FakeSlackAdapter`` extended with ``send_admin_channel``,
``_FakePromptRenderer``, ``_FakeNotificationLogStore``); no Postgres,
no real network.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from notification import (
    NotificationError,
    NotificationLogEntry,
    NotificationService,
    TemplateRenderError,
)

from .test_notify_workflow_completion import (
    _FakeEmailAdapter,
    _FakeNotificationLogStore,
    _FakePromptRenderer,
    _FakeSlackAdapter,
)


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
    """Build a :class:`NotificationService` wired to fresh in-memory fakes.

    The default :class:`_FakePromptRenderer` carries the
    ``notifications/audit_prune_failed`` template; tests that need a
    missing-template scenario inject a renderer with empty bodies.
    """

    slack = slack or _FakeSlackAdapter()
    email = email or _FakeEmailAdapter()
    prompts = prompts or _FakePromptRenderer(
        bodies={
            "notifications/audit_prune_failed": (
                ":rotating_light: AuditPruneWorkflow failed: {error}"
            ),
        }
    )
    log_store = log_store or _FakeNotificationLogStore()
    service = NotificationService(
        slack=slack,
        email=email,
        prompts=prompts,
        log_store=log_store,
    )
    return service, slack, email, prompts, log_store


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Happy path: admin alarm fires regardless of dept config
# ---------------------------------------------------------------------------


def test_alarm_posts_to_admin_channel_with_alert_type() -> None:
    """R6.4 — admin Slack channel receives the alarm with the pinned alert_type."""

    service, slack, email, prompts, _ = _service_with_fakes()

    outcome = _run(service.notify_audit_prune_failed(error="MinIO 503"))

    # Body rendered exactly once via the audit_prune_failed template.
    assert prompts.render_calls == [
        ("notifications/audit_prune_failed", {"error": "MinIO 503"}),
    ]
    # Admin channel got the rendered body + the pinned alert_type.
    assert len(slack.admin_sends) == 1
    body, alert_type = slack.admin_sends[0]
    assert "MinIO 503" not in body  # fake renderer doesn't substitute, but
    # the renderer was called with the error in vars; the substitution is
    # the renderer's job in production.
    assert alert_type == "audit_prune_failed"
    # Dept-scoped Slack send was never invoked.
    assert slack.sends == []
    # Email channel never invoked (admin alarm is Slack-only).
    assert email.sends == []
    # Outcome flags.
    assert outcome.slack_sent is True
    assert outcome.slack_failed is False
    assert outcome.slack_skipped_dedup is False
    assert outcome.email_sent is False


def test_alarm_writes_one_notification_log_row() -> None:
    """Property 10 (d) parity — the alarm carries a ``notification_log`` row."""

    service, _, _, _, store = _service_with_fakes()

    _run(service.notify_audit_prune_failed(error="boom"))

    [row] = store.rows
    assert isinstance(row, NotificationLogEntry)
    assert row.channel == "slack"
    assert row.kind == "audit_prune_failed"
    assert row.status == "sent"
    assert row.error is None


# ---------------------------------------------------------------------------
# dedup_key shape (reuse of workflow-completion machinery)
# ---------------------------------------------------------------------------


def test_dedup_key_default_run_id_matches_documented_shape() -> None:
    """Default ``run_id`` hashes ``audit-prune-cron:slack:audit_prune_failed``."""

    service, _, _, _, store = _service_with_fakes()

    _run(service.notify_audit_prune_failed(error="oops"))

    expected = hashlib.sha256(
        b"audit-prune-cron:slack:audit_prune_failed"
    ).hexdigest()
    assert [r.dedup_key for r in store.rows] == [expected]


def test_dedup_key_custom_run_id_is_reflected_in_hash() -> None:
    """Per-cron-run idempotency — ``run_id`` substitutes the workflow_id slot."""

    service, _, _, _, store = _service_with_fakes()

    _run(
        service.notify_audit_prune_failed(
            error="oops",
            run_id="audit-prune-cron-2024-01-15",
        )
    )

    expected = hashlib.sha256(
        b"audit-prune-cron-2024-01-15:slack:audit_prune_failed"
    ).hexdigest()
    assert [r.dedup_key for r in store.rows] == [expected]


def test_idempotent_retry_skips_second_admin_send() -> None:
    """Property 10 (d) — same run, second attempt is a no-op send."""

    service, slack, _, _, store = _service_with_fakes()

    outcome_a = _run(
        service.notify_audit_prune_failed(error="boom", run_id="run-1")
    )
    outcome_b = _run(
        service.notify_audit_prune_failed(error="boom", run_id="run-1")
    )

    # Adapter saw exactly one admin send across two calls.
    assert len(slack.admin_sends) == 1
    # Log table has exactly one row (UNIQUE(dedup_key) rejected the second).
    assert len(store.rows) == 1
    # Outcome reflects the retry path.
    assert outcome_a.slack_sent is True
    assert outcome_b.slack_sent is False
    assert outcome_b.slack_skipped_dedup is True


def test_distinct_run_ids_produce_distinct_dedup_keys() -> None:
    """Two cron runs ⇒ two rows; hashes differ."""

    service, slack, _, _, store = _service_with_fakes()

    _run(service.notify_audit_prune_failed(error="boom", run_id="run-1"))
    _run(service.notify_audit_prune_failed(error="boom", run_id="run-2"))

    assert len(slack.admin_sends) == 2
    keys = {r.dedup_key for r in store.rows}
    assert len(keys) == 2


# ---------------------------------------------------------------------------
# Target redaction
# ---------------------------------------------------------------------------


def test_log_row_records_admin_channel_label_not_webhook_url() -> None:
    """Foundation R7.8 parity — webhook URL never lands in the table.

    The admin webhook URL is resolved by the adapter from
    ``vault:notifications/slack/admin`` and never crosses the dispatcher
    boundary, so the log row carries the stable ``"admin-channel"``
    label instead of a hashed URL. Audits can still query by
    ``WHERE target = 'admin-channel'`` without learning the webhook
    secret.
    """

    service, _, _, _, store = _service_with_fakes()

    _run(service.notify_audit_prune_failed(error="boom"))

    [row] = store.rows
    assert row.target == "admin-channel"
    # Body hash is the sha256 of the rendered body (foundation parity:
    # plaintext bodies never land in the table).
    expected_body = ":rotating_light: AuditPruneWorkflow failed: {error}"
    assert row.body_hash == hashlib.sha256(
        expected_body.encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_render_failure_raises_template_render_error() -> None:
    """Missing prompt template ⇒ ``TemplateRenderError`` (never retryable)."""

    prompts = _FakePromptRenderer(bodies={})  # template missing
    service, slack, _, _, store = _service_with_fakes(prompts=prompts)

    with pytest.raises(TemplateRenderError):
        _run(service.notify_audit_prune_failed(error="boom"))

    # No log row, no admin send — the dispatcher fails before
    # touching either side effect.
    assert store.rows == []
    assert slack.admin_sends == []


def test_admin_send_transport_failure_is_reraised() -> None:
    """Mandatory alarm is never best-effort — adapter failure re-raises."""

    slack = _FakeSlackAdapter(raise_on_admin_send=RuntimeError("slack 503"))
    service, _, _, _, store = _service_with_fakes(slack=slack)

    with pytest.raises(NotificationError) as exc_info:
        _run(service.notify_audit_prune_failed(error="boom"))

    # The wrapped error mentions the underlying transport error.
    assert "slack 503" in str(exc_info.value)
    # The log row landed (optimistic insert before the adapter call); the
    # store's UNIQUE(dedup_key) will dedupe a successful retry.
    assert len(store.rows) == 1
    assert store.rows[0].channel == "slack"
    assert store.rows[0].kind == "audit_prune_failed"


def test_failed_send_then_dedup_on_retry() -> None:
    """Dedup machinery still works after a transport-failed first attempt.

    First attempt: insert lands, adapter raises, dispatcher re-raises.
    Second attempt with the same ``run_id``: insert returns False
    (UNIQUE collision), adapter is NOT called again — the dispatcher
    returns the dedup-skip outcome instead of re-raising.

    This mirrors the workflow-completion contract: dedup is decided by
    the store's ``UNIQUE(dedup_key)`` constraint, independent of whether
    the previous attempt's adapter send succeeded.
    """

    slack = _FakeSlackAdapter(raise_on_admin_send=RuntimeError("slack 503"))
    service, _, _, _, store = _service_with_fakes(slack=slack)

    # First attempt: fails through the adapter.
    with pytest.raises(NotificationError):
        _run(service.notify_audit_prune_failed(error="boom", run_id="r-1"))

    # Heal the adapter; second attempt should still dedup (the
    # dedup_key is already in the store from attempt 1).
    slack.raise_on_admin_send = None
    outcome = _run(
        service.notify_audit_prune_failed(error="boom", run_id="r-1")
    )

    # Adapter was never called on the second attempt.
    assert slack.admin_sends == []
    # Outcome reports the dedup-skip path.
    assert outcome.slack_skipped_dedup is True
    assert outcome.slack_sent is False
    # Store still has exactly one row.
    assert len(store.rows) == 1


# ---------------------------------------------------------------------------
# Independence from dept config + workflow-completion path
# ---------------------------------------------------------------------------


def test_alarm_does_not_consult_dept_config() -> None:
    """The admin alarm is dept-agnostic — no DeptConfigView is accepted."""

    # Inspect the public method signature: it MUST NOT accept ``dept``.
    import inspect

    sig = inspect.signature(NotificationService.notify_audit_prune_failed)
    assert "dept" not in sig.parameters
    # Only ``self``, ``error`` and ``run_id`` are part of the contract.
    assert set(sig.parameters) == {"self", "error", "run_id"}


def test_alarm_does_not_call_dept_send_or_email() -> None:
    """Even when dept channels exist in the suite, ``notify_audit_prune_failed``
    routes only through ``send_admin_channel`` — never ``send`` or email."""

    service, slack, email, _, _ = _service_with_fakes()

    _run(service.notify_audit_prune_failed(error="boom"))

    assert slack.sends == []  # dept-scoped Slack channel never touched
    assert email.sends == []  # email channel never touched
    assert len(slack.admin_sends) == 1
