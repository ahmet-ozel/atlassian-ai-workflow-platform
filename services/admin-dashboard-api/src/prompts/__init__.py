"""Prompt-management sub-package for admin-dashboard-api.

Hosts deterministic helpers that the ``/admin/prompts`` HTTP layer
(:mod:`src.routers.prompts_git`) depends on. The current modules:

* :mod:`.pr_renderer` — canonical Markdown PR description renderer
  used when the caller of ``POST /admin/prompts/{path}/pr`` does not
  override the description. The renderer is a pure function: every
  input is passed in, no app state is read, no I/O is performed.

The package is intentionally framework-agnostic so the renderer can
be exercised by unit tests without standing up FastAPI or git.
"""

from __future__ import annotations

from .audit_writer import AsyncpgAuditEventsWriter, AsyncpgAuditSink
from .pr_renderer import (
    PR_DESCRIPTION_HEADER,
    SandboxRunSummary,
    V15SyncStatus,
    extract_v15_status,
    render_pr_description,
)

__all__ = [
    "AsyncpgAuditEventsWriter",
    "AsyncpgAuditSink",
    "PR_DESCRIPTION_HEADER",
    "SandboxRunSummary",
    "V15SyncStatus",
    "extract_v15_status",
    "render_pr_description",
]
