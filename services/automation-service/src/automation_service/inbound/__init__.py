"""Inbound channel adapters for the automation-service (task 8.5, B19).

Implements `Requirement 5.10 <../../../.kiro/specs/platform-mimari-ops/requirements.md>`_
— **Slack/Email-to-task adapter (B19, MIMARI §16.12)**.

These adapters listen on external channels (Slack incoming webhook,
IMAP mailbox) and, when a user mentions the bot or sends a request to
the configured inbox, create a Jira task via the *standard task-creator
path*. The actual Jira issue creation is delegated to Temporal
(``AutomationWorkflow``) which is the canonical "auto-assign +
smart-defaults" surface (Requirement 3.5, design §"Komponent Sahipliği")
— inbound adapters never call the Atlassian MCP directly so the
bot-loop guard, capability gate and audit chain stay deterministic.

Two surface types are provided:

* :mod:`slack_to_task` — synchronous FastAPI router exposing
  ``POST /webhooks/inbound/slack``. Slack's incoming-webhook contract
  (signed with ``X-Slack-Signature`` over ``v0:<ts>:<body>``) is
  verified up-front; the parsed mention is forwarded to the workflow
  client.
* :mod:`email_to_task` — asynchronous IMAP poller. Configured by the
  ``EMAIL_INBOUND_ADDRESS`` env var, it polls the mailbox at a fixed
  cadence, classifies messages whose ``To:`` matches the configured
  address, and starts a workflow per accepted message.

Both adapters share the same downstream contract (``InboundTaskRequest``
→ ``start_workflow_idempotent``) so the audit / RBAC / loop-guard
behaviour is identical regardless of channel.
"""

from __future__ import annotations

from .common import (
    InboundContext,
    InboundDeptResolver,
    InboundTaskRequest,
    SlackSignatureVerifier,
    auto_assign_workflow_input,
    build_inbound_workflow_id,
    utc_now,
)
from .email_to_task import EmailInboundConfig, EmailToTaskPoller
from .slack_to_task import router as slack_router

__all__ = [
    # Shared types
    "InboundContext",
    "InboundDeptResolver",
    "InboundTaskRequest",
    "SlackSignatureVerifier",
    "auto_assign_workflow_input",
    "build_inbound_workflow_id",
    "utc_now",
    # Slack
    "slack_router",
    # Email
    "EmailInboundConfig",
    "EmailToTaskPoller",
]
