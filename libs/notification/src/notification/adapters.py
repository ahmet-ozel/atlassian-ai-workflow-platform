"""Protocol-typed adapter contracts for the notification dispatcher.

The notification service depends on three :class:`~typing.Protocol`s instead
of concrete classes so:

* Sibling task 8.1 can drop in the ``aiohttp`` Slack adapter and the
  ``aiosmtplib`` email adapter without re-touching this module.
* Tests for task 8.2 (this PR) can inject lightweight in-memory fakes that
  capture every dispatch call and the row that lands in
  ``shared.notification_log`` — no Postgres, no real network.

The protocols are intentionally minimal — only the calls
:meth:`NotificationService.notify_workflow_completion` makes are part of
the contract. Anything else (rate limiting, retry policy, vault
resolution) lives inside the concrete implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .types import (
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
)

__all__ = [
    "EmailAdapter",
    "NotificationLogEntry",
    "NotificationLogStore",
    "PromptRenderer",
    "SlackAdapter",
]


# ---------------------------------------------------------------------------
# Adapter protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class SlackAdapter(Protocol):
    """Minimal Slack send surface.

    Sibling task 8.1's concrete implementation uses ``aiohttp`` POST against
    the resolved webhook with a 1 msg/sec/channel token bucket. Tests for
    8.2 / 8.3 use a list-backed fake whose ``send`` / ``send_admin_channel``
    simply append the call arguments for later assertion.

    Two methods, two destinations:

    * :meth:`send` — dept-scoped notifications. The webhook is the dept's
      Slack channel resolved from ``vault:notifications/{dept_id}/slack``
      (sibling lib ``vault_client``); the caller passes the resolved URL.
    * :meth:`send_admin_channel` — platform-wide admin alarms (sibling task
      8.3). The webhook is **always** the admin channel resolved by the
      adapter from ``vault:notifications/slack/admin``; callers do not pass
      a webhook because the destination is fixed by the design (R6.4 — the
      ``audit_prune_failed`` alarm must reach the ops admin channel
      regardless of any dept config).
    """

    async def send(self, body: str, *, webhook: str) -> None:
        """POST ``body`` to ``webhook``.

        Concrete implementations may raise :class:`Exception` on transport
        / 4xx-5xx failures; :meth:`NotificationService.notify_workflow_completion`
        catches the failure, records it as ``status="failed"`` in
        ``shared.notification_log`` and re-raises so the caller's retry
        policy can take over.
        """

        ...

    async def send_admin_channel(self, body: str, *, alert_type: str) -> None:
        """POST ``body`` to the admin Slack channel (R6.4).

        The destination webhook is **fixed** to the vault-resolved value at
        ``vault:notifications/slack/admin`` and is intentionally *not*
        configurable per call — this is the platform's mandatory ops
        alarm channel and must not be overridden by dept config (S8 /
        Property 10 invariant: "AuditPruneWorkflow fail'i sessizleştirilemez").

        Args:
            body: Rendered alarm body (typically from the
                ``notifications/audit_prune_failed`` template).
            alert_type: Stable identifier of the alarm class (e.g.
                ``"audit_prune_failed"``). Concrete implementations may use
                this to drive Slack ``blocks`` formatting and/or emit a
                ``slack_admin_alert_total{alert_type=...}`` Prometheus
                counter. Tests assert the value verbatim to verify the
                correct call site fired.

        Concrete implementations may raise on transport failures; the
        caller (Temporal activity) decides whether to retry.
        """

        ...


@runtime_checkable
class EmailAdapter(Protocol):
    """Minimal email send surface.

    Sibling task 8.1's implementation uses ``aiosmtplib`` against the SMTP
    credential resolved from ``vault:notifications/smtp/credential``. Tests
    use a list-backed fake.
    """

    async def send(self, body: str, *, to: str) -> None:
        """Send ``body`` to the RFC-5322 address ``to``."""

        ...


@runtime_checkable
class PromptRenderer(Protocol):
    """Render a prompt body by logical name.

    The notification service consumes a *narrowed* slice of
    :class:`prompts.loader.PromptLoader` — only :meth:`render` is part of
    the contract. Defining a Protocol here keeps the lib free of a hard
    dependency on :mod:`prompts` (which would force every consumer to
    install the prompts loader even when they only need the dispatcher's
    in-memory fake in tests).

    The ``vars`` argument is intentionally typed as :class:`object` so the
    protocol works with both :class:`prompts.types.PromptVars` and the
    notification-specific ``PromptVars`` extension carrying
    ``workflow_id`` / ``error`` placeholders. The concrete renderer
    decides whether the value is acceptable.
    """

    def render(self, name: str, *, vars: object) -> str:
        """Render the prompt at logical ``name`` with ``vars`` substituted in."""

        ...


# ---------------------------------------------------------------------------
# notification_log store
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotificationLogEntry:
    """Row shape for ``shared.notification_log`` inserts.

    Mirrors the column set declared in
    ``infra/postgres/20_ops.sql``:

    .. code-block:: sql

        CREATE TABLE shared.notification_log (
            id          BIGSERIAL PRIMARY KEY,
            dedup_key   TEXT NOT NULL UNIQUE,
            channel     TEXT NOT NULL,
            kind        TEXT NOT NULL,
            target      TEXT NOT NULL,
            body_hash   TEXT NOT NULL,
            status      TEXT NOT NULL,
            error       TEXT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );

    The ``id`` and ``created_at`` columns are populated by Postgres so
    they are not part of this dataclass — the store implementation is
    responsible for not passing them through to the SQL emitter.

    Frozen + ``slots=True`` so the entry is a value object that the
    dispatcher hashes / compares without surprise mutation between
    "I built this row" and "I sent it to the store".
    """

    dedup_key: str
    channel: NotificationChannel
    kind: NotificationKind
    target: str
    body_hash: str
    status: NotificationStatus
    error: str | None = None


@runtime_checkable
class NotificationLogStore(Protocol):
    """Persistence surface for ``shared.notification_log``.

    The protocol exposes a single :meth:`insert` call that takes a fully
    populated :class:`NotificationLogEntry` and returns ``True`` when the
    row landed (``status='sent'`` / ``'failed'`` / ``'retrying'`` written
    successfully) or ``False`` when ``dedup_key`` already existed (i.e.
    the ``UNIQUE`` constraint rejected the insert). Returning a boolean
    instead of raising on duplicate-key keeps the dispatch path branch-free
    — the call site simply skips the adapter send when ``False``.

    Concrete implementations (sibling task 8.1) translate this contract to
    the Postgres ``INSERT INTO shared.notification_log ... ON CONFLICT
    (dedup_key) DO NOTHING RETURNING id`` idiom; the boolean is the
    ``RETURNING id IS NOT NULL`` projection.
    """

    async def insert(self, entry: NotificationLogEntry) -> bool:
        """Persist ``entry`` to ``shared.notification_log``.

        Returns:
            ``True`` if the row was newly inserted, ``False`` if an
            existing row already has the same ``dedup_key`` (idempotent
            retry — the caller MUST skip the adapter send).
        """

        ...
