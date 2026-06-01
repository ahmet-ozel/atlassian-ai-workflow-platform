"""Microsoft Teams notification adapter.

Sends notifications via Teams webhook URL with retry logic.

Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Any
import httpx

__all__ = ("TeamsAdapter", "TeamsNotification", "send_teams_notification")

_logger = logging.getLogger(__name__)

TEAMS_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 3
RETRY_INTERVAL_SECONDS = 5.0

@dataclass(frozen=True)
class TeamsNotification:
    webhook_url: str
    title: str
    body: str
    color: str = "0076D7"  # Blue

@dataclass(frozen=True)
class NotificationResult:
    success: bool
    channel: str
    error: str | None = None
    attempts: int = 1

class TeamsAdapter:
    async def send(self, notification: TeamsNotification) -> NotificationResult:
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": notification.color,
            "summary": notification.title,
            "sections": [{
                "activityTitle": notification.title,
                "text": notification.body,
            }],
        }

        last_error: str | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=TEAMS_TIMEOUT_SECONDS) as client:
                    response = await client.post(notification.webhook_url, json=payload)
                    if response.status_code < 300:
                        return NotificationResult(success=True, channel="teams", attempts=attempt)
                    last_error = f"HTTP {response.status_code}"
            except Exception as e:
                last_error = str(e)

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_INTERVAL_SECONDS)

        _logger.warning("Teams notification failed after %d attempts: %s", MAX_RETRIES, last_error)
        return NotificationResult(success=False, channel="teams", error=last_error, attempts=MAX_RETRIES)

async def send_teams_notification(webhook_url: str, title: str, body: str) -> NotificationResult:
    adapter = TeamsAdapter()
    return await adapter.send(TeamsNotification(webhook_url=webhook_url, title=title, body=body))
