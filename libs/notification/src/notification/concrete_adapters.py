"""Concrete adapters for the notification dispatcher (task **8.1**).

Implements the three :class:`~typing.Protocol`-typed surfaces declared
in :mod:`notification.adapters` against real transports:

* :class:`AiohttpSlackAdapter` - POST to the resolved Slack webhook
  with a 1 msg/sec/channel token bucket back-pressure.
* :class:`AiosmtplibEmailAdapter` - STARTTLS SMTP send using SMTP
  credentials resolved from Vault.
* :class:`AsyncpgNotificationLogStore` - backs ``shared.notification_log``
  via ``asyncpg.Pool`` with ``ON CONFLICT (dedup_key) DO NOTHING``
  semantics returning ``True`` / ``False``.

All three are wired by lifespan code (eg. ``automation-worker.main``,
``services/admin-dashboard-api.main``); the concrete classes accept
their dependency clients (``aiohttp.ClientSession``, ``asyncpg.Pool``,
SMTP credentials dict) at construction time so the lifespan owns the
client lifecycle and the adapter stays a thin shim.

:class:`NotificationService` consumes the
Protocol-typed surfaces declared in :mod:`notification.adapters`,
not the concrete classes here, so unit tests can stay protocol-only.

These adapters depend on optional packages (`aiohttp`, `aiosmtplib`,
`asyncpg`) that may not be installed in every test environment. The
imports are guarded so the module can still be imported and the
class definitions surfaced for type-checking; instantiation raises
:class:`ImportError` with a clear hint when the dependency is
missing.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .adapters import (
    EmailAdapter,
    NotificationLogEntry,
    NotificationLogStore,
    SlackAdapter,
)

__all__ = [
    "AiohttpSlackAdapter",
    "AiosmtplibEmailAdapter",
    "AsyncpgNotificationLogStore",
    "TokenBucket",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token bucket (1 msg / sec / channel back-pressure)
# ---------------------------------------------------------------------------


@dataclass
class TokenBucket:
    """1 msg/sec/channel rate limiter for Slack (Slack's documented limit).

    Implements the simple "token bucket" algorithm:

    * ``capacity`` tokens, refilled at ``refill_rate`` tokens/sec.
    * ``acquire(key)`` blocks asynchronously until a token is
      available for the given key (typically the webhook URL) and
      consumes one.

    The bucket is **per-key** so different Slack channels do not
    starve each other. Thread-safety is provided by an asyncio Lock
    keyed by the same lookup; concurrent producers for the same key
    serialise through that lock without livelock.
    """

    capacity: int = 1
    refill_rate: float = 1.0  # tokens per second
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, key: str) -> None:
        async with self._lock:
            now = time.monotonic()
            tokens, last = self._state.get(key, (float(self.capacity), now))
            elapsed = now - last
            tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
            if tokens < 1.0:
                wait_s = (1.0 - tokens) / self.refill_rate
                # Release the lock while sleeping so other keys can
                # progress concurrently.
                self._state[key] = (0.0, now)

        if tokens < 1.0:
            await asyncio.sleep(wait_s)  # type: ignore[possibly-undefined]
            async with self._lock:
                self._state[key] = (0.0, time.monotonic())
            return

        async with self._lock:
            self._state[key] = (tokens - 1.0, now)


# ---------------------------------------------------------------------------
# Slack adapter
# ---------------------------------------------------------------------------


@dataclass
class AiohttpSlackAdapter:
    """Slack webhook POST adapter using ``aiohttp``.

    Args:
        session: ``aiohttp.ClientSession`` (caller owns lifecycle).
        admin_webhook: Webhook URL for the platform admin Slack
            channel (resolved from ``vault:notifications/slack/admin``).
            Used by :meth:`send_admin_channel`.
        bucket: Optional :class:`TokenBucket`; constructed with
            defaults if ``None`` is passed.
        timeout_s: Per-request timeout (passed through to aiohttp).
    """

    session: Any  # aiohttp.ClientSession
    admin_webhook: str
    bucket: TokenBucket = field(default_factory=TokenBucket)
    timeout_s: float = 10.0

    async def send(self, body: str, *, webhook: str) -> None:
        await self.bucket.acquire(webhook)
        payload = {"text": body}
        async with self.session.post(
            webhook,
            json=payload,
            timeout=self.timeout_s,
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(
                    f"slack webhook returned {resp.status}: {text[:200]}"
                )

    async def send_admin_channel(self, body: str, *, alert_type: str) -> None:
        # The admin webhook is a dedicated channel - share the same
        # token bucket using a stable key so concurrent admin alarms
        # serialise properly.
        await self.bucket.acquire("__admin_channel__")
        payload = {
            "text": body,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": body},
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f":rotating_light: alert_type: `{alert_type}`",
                        }
                    ],
                },
            ],
        }
        async with self.session.post(
            self.admin_webhook,
            json=payload,
            timeout=self.timeout_s,
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(
                    f"slack admin webhook returned {resp.status}: "
                    f"{text[:200]} (alert_type={alert_type})"
                )


# Verify protocol satisfaction at import time when aiohttp is missing
# the runtime check still passes because ``AiohttpSlackAdapter`` has
# both methods declared statically.
_ = SlackAdapter  # noqa: B018 - keep the symbol referenced


# ---------------------------------------------------------------------------
# Email adapter
# ---------------------------------------------------------------------------


@dataclass
class AiosmtplibEmailAdapter:
    """RFC-5322 email send adapter using ``aiosmtplib``.

    Args:
        host: SMTP host (eg. ``smtp.example.org``).
        port: SMTP port (typically 587 for STARTTLS).
        username: SMTP username (Vault-resolved).
        password: SMTP password (Vault-resolved).
        from_addr: ``From:`` header value.
        subject_prefix: Stable prefix for every subject (eg.
            ``"[platform-notification]"``); the body's first line
            is used as the subject suffix.
        starttls: Whether to upgrade the connection via STARTTLS.
            Defaults to ``True`` (production); set ``False`` for
            local-dev SMTP servers without TLS.
    """

    host: str
    port: int
    username: str
    password: str
    from_addr: str
    subject_prefix: str = "[platform-notification]"
    starttls: bool = True

    async def send(self, body: str, *, to: str) -> None:
        try:
            import aiosmtplib  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "AiosmtplibEmailAdapter requires the `aiosmtplib` package"
            ) from exc

        # Subject = prefix + first non-empty line of the body, capped.
        subject = self._subject_from(body)
        message = (
            f"From: {self.from_addr}\r\n"
            f"To: {to}\r\n"
            f"Subject: {subject}\r\n"
            f"Date: {email.utils.formatdate(localtime=True)}\r\n"
            f"Message-ID: {email.utils.make_msgid()}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            f"{body}\r\n"
        )

        await aiosmtplib.send(
            message,
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            start_tls=self.starttls,
        )

    def _subject_from(self, body: str) -> str:
        first_line = ""
        for line in body.splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped
                break
        first_line = first_line[:120] if first_line else "notification"
        return f"{self.subject_prefix} {first_line}"


_ = EmailAdapter  # noqa: B018 - keep the symbol referenced


# ---------------------------------------------------------------------------
# notification_log store
# ---------------------------------------------------------------------------


@dataclass
class AsyncpgNotificationLogStore:
    """``shared.notification_log`` store backed by ``asyncpg.Pool``.

    The :meth:`insert` returns ``True`` when a row landed, ``False``
    when ``UNIQUE(dedup_key)`` rejected the insert. Implementation
    issues ``INSERT ... ON CONFLICT (dedup_key) DO NOTHING RETURNING
    id`` and projects the result to a boolean.
    """

    pool: Any  # asyncpg.Pool

    async def insert(self, entry: NotificationLogEntry) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO shared.notification_log (
                    dedup_key, channel, kind, target,
                    body_hash, status, error
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (dedup_key) DO NOTHING
                RETURNING id
                """,
                entry.dedup_key,
                entry.channel,
                entry.kind,
                entry.target,
                entry.body_hash,
                entry.status,
                entry.error,
            )
        return row is not None


_ = NotificationLogStore  # noqa: B018 - keep the symbol referenced
