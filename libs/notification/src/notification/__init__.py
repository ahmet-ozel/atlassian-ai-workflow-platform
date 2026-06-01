"""Public surface of the ``libs/notification`` package.

The dispatch policy implemented here mirrors
``.kiro/specs/platform-mimari-ops/design.md`` §`NotificationService`:

* :func:`NotificationService.notify_workflow_completion` is **success-gated**
  (`dept.notify_on_success == False` ⇒ no-op for non-failure outcomes) and
  **failure-mandatory** (every ``status == "failed"`` workflow notifies the
  dept's Slack channel regardless of dept config).
* Each dispatch attempt writes one row to ``shared.notification_log`` whose
  ``dedup_key`` (sha256 of ``workflow_id`` + ``channel`` + ``kind``) is
  ``UNIQUE`` — a retried call cannot double-deliver.

Task 8.1 (sibling) provides the concrete ``aiohttp`` / ``aiosmtplib`` adapter
implementations; this package only depends on the
:class:`SlackAdapter` / :class:`EmailAdapter` /
:class:`NotificationLogStore` protocols defined in :mod:`notification.adapters`.
"""

from __future__ import annotations

from .adapters import (
    EmailAdapter,
    NotificationLogEntry,
    NotificationLogStore,
    PromptRenderer,
    SlackAdapter,
)
from .concrete_adapters import (
    AiohttpSlackAdapter,
    AiosmtplibEmailAdapter,
    AsyncpgNotificationLogStore,
    TokenBucket,
)
from .errors import NotificationError, TemplateRenderError
from .service import NotificationOutcome, NotificationService
from .types import (
    DeptConfigView,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
    WorkflowResult,
    WorkflowStatus,
)

__all__ = [
    "AiohttpSlackAdapter",
    "AiosmtplibEmailAdapter",
    "AsyncpgNotificationLogStore",
    "DeptConfigView",
    "EmailAdapter",
    "NotificationChannel",
    "NotificationError",
    "NotificationKind",
    "NotificationLogEntry",
    "NotificationLogStore",
    "NotificationOutcome",
    "NotificationService",
    "NotificationStatus",
    "PromptRenderer",
    "SlackAdapter",
    "TemplateRenderError",
    "TokenBucket",
    "WorkflowResult",
    "WorkflowStatus",
]
