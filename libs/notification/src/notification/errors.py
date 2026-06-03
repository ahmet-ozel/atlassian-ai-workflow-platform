"""Notification dispatcher exception hierarchy.

The two leaf exceptions distinguish *which side* of the dispatch failed so
callers (typically a Temporal activity) can apply different retry policies:

* :class:`TemplateRenderError` — the prompt template could not be rendered
  (missing placeholder, unknown prompt name, escape error). This is **never**
  retryable — the workflow should fail-fast and surface the validation error
  to the operator.
* :class:`NotificationError` — generic transport / persistence failure. Used
  by concrete adapters when Slack returns non-2xx, SMTP times out,
  or the Postgres ``notification_log`` insert fails for a non-unique reason.

The base :class:`NotificationError` is also the catch-all that
:meth:`NotificationService.notify_workflow_completion` callers use when they
only care about "did the dispatch succeed?".
"""

from __future__ import annotations


class NotificationError(Exception):
    """Base class for every notification dispatcher failure."""


class TemplateRenderError(NotificationError, ValueError):
    """Raised when the underlying :class:`PromptRenderer` could not render a body.

    Wraps the loader's :class:`prompts.errors.PromptTemplateError` /
    :class:`prompts.errors.PromptNotFoundError` so callers depending on
    :mod:`notification` do not need a transitive import on :mod:`prompts`
    just to handle render failures.

    Inherits from :class:`ValueError` so generic exception handlers that
    already special-case ``ValueError`` (eg. FastAPI request validation,
    Temporal non-retryable errors) can catch it without explicit imports.
    """
