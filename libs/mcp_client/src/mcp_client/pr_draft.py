"""PR draft enforcement.

When an LLM (or any other caller) hands us a pull request creation
payload whose ``draft`` field is ``False`` or absent, this module
rewrites it to ``draft=True`` and writes a single
``pr_draft_enforced`` audit event so operators can trace which call
the platform had to flip.

The function is the **single source of truth** for the rule. Callers
that build PR payloads (:mod:`automation_service`, MCP tool
interceptors in this lib, future admin-dashboard tooling) must route
every outgoing payload through :func:`enforce_pr_draft` before
handing it off to a transport.

The enforcement is unconditional — we never trust the caller's intent
on this field. Tests verify the universal property for arbitrary inputs.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final, Mapping

if TYPE_CHECKING:
    # Imported lazily / under TYPE_CHECKING so ``audit_logger`` stays an
    # optional runtime dependency. Callers that don't pass an
    # ``audit_logger`` argument never trigger the import.
    from audit_logger import AuditEvent, AuditLogger, AuditRole


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Audit ``action`` value emitted whenever the function had to flip a
#: ``False`` (or missing) ``draft`` field.
PR_DRAFT_AUDIT_ACTION: Final[str] = "pr_draft_enforced"


# ---------------------------------------------------------------------------
# enforce_pr_draft — coerce ``draft`` to True
# ---------------------------------------------------------------------------


async def enforce_pr_draft(
    payload: Mapping[str, Any],
    *,
    audit_logger: "AuditLogger | None" = None,
    actor_id: str = "system",
    actor_role: "AuditRole" = "system",
    dept_id: str | None = None,
    resource: str | None = None,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Return a copy of ``payload`` with ``draft`` forced to ``True``.

    Every outgoing PR payload must have ``draft=True``. The function is
    unconditional — if the caller's
    payload says ``draft=False`` (or omits the field) we overwrite it
    and emit a ``pr_draft_enforced`` audit event so the override is
    traceable.

    Args:
        payload: The PR creation payload an LLM (or other caller)
            produced. Treated as immutable input — the function
            performs a deep-ish copy so mutations to the returned
            ``dict`` do not affect the caller's mapping.
        audit_logger: Optional :class:`audit_logger.AuditLogger`. When
            supplied and the function had to flip the field, an
            :class:`audit_logger.AuditEvent` with
            ``action="pr_draft_enforced"`` is written via the logger.
            Passing ``None`` keeps the function usable in tests / pure
            call paths.
        actor_id: ``actor_id`` to record on the audit event. Defaults
            to ``"system"`` for background interceptor call paths.
        actor_role: ``actor_role`` to record on the audit event.
            Defaults to ``"system"``.
        dept_id: Optional ``dept_id`` to record on the audit event.
            ``None`` is acceptable for cross-department system flows.
        resource: Optional resource identifier (eg.
            ``"bitbucket:workspace/repo"``) to record on the audit
            event. ``None`` falls back to the literal ``"pr"``.
        timestamp: Optional UTC timestamp to record on the audit
            event. Defaults to :func:`datetime.now` in UTC.

    Returns:
        A new ``dict`` mirroring ``payload`` with ``draft=True``. The
        function never returns the input mapping itself — callers can
        mutate the result without aliasing concerns.

    Notes:
        The audit event is written **only when the rule had to flip
        the field** (``draft`` was ``False`` or missing). Calls that
        already pass ``draft=True`` are a no-op on the audit log; the
        rule is still enforced (the function still re-asserts ``True``
        on the copy) but no operator-facing event is produced.

    Example::

        >>> import asyncio
        >>> result = asyncio.run(
        ...     enforce_pr_draft({"title": "Fix bug", "draft": False})
        ... )
        >>> result["draft"]
        True
        >>> result["title"]
        'Fix bug'
    """

    # ``copy.deepcopy`` keeps nested structures (eg. reviewer arrays,
    # branch descriptors) isolated from the caller's mapping. The
    # payloads we expect here are small (≤ a few KB) so the cost is
    # negligible compared to the safety win.
    coerced: dict[str, Any] = copy.deepcopy(dict(payload))

    original_draft: Any = coerced.get("draft", _MISSING)
    needed_flip: bool = original_draft is _MISSING or original_draft is not True
    coerced["draft"] = True

    if needed_flip and audit_logger is not None:
        # Imported lazily so the audit dependency stays optional —
        # callers that pass ``audit_logger=None`` don't need
        # ``audit_logger`` installed at all.
        from audit_logger import AuditEvent

        event = AuditEvent(
            actor_id=actor_id,
            actor_role=actor_role,
            dept_id=dept_id,
            action=PR_DRAFT_AUDIT_ACTION,
            resource=resource if resource is not None else "pr",
            result="ok",
            timestamp=timestamp
            if timestamp is not None
            else datetime.now(timezone.utc),
            payload={"original_draft": _audit_value(original_draft)},
        )
        await audit_logger.write(event)

    return coerced


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Sentinel sentinel used to distinguish "field was absent" from
#: "field was explicitly ``None``". A bare ``object()`` is sufficient —
#: the value is never exposed to callers.
_MISSING: Final[object] = object()


def _audit_value(value: Any) -> Any:
    """Convert the missing-sentinel to ``None`` for the audit payload.

    The audit row's ``payload`` JSON column expects JSON-serialisable
    values; the private :data:`_MISSING` sentinel is not. We round
    -trip it to ``None`` so downstream consumers see a well-defined
    value.
    """

    return None if value is _MISSING else value
