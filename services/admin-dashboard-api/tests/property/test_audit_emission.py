"""- Each mutation/test emits exactly one audit event.
``ProviderService``:
* exactly one :class:`AuditEvent` is emitted, with
  ``actor_role="admin"`` and ``actor_id=claims.sub``;
* the serialised event carries no unredacted credential markers; and
* if the sink raises, the underlying HTTP response code is unchanged
  and the failure surfaces as a WARNING log line."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _llm_providers_fakes import (  # noqa: E402
    RecordingAuditSink,
    build_service,
)
from src.llm_providers.schemas import (  # noqa: E402
    ProviderUpdate,
    UnsavedTestRequest,
)
from src.llm_providers.service import _ProviderCreateInput  # noqa: E402


# ---------------------------------------------------------------------------
# Exactly one audit event per mutation/test branch.
# ---------------------------------------------------------------------------


def _serialise(event: Any) -> str:
    """Serialise an :class:`AuditEvent` to a JSON string for marker checks."""

    envelope: dict[str, Any] = {
        "actor_id": event.actor_id,
        "actor_role": event.actor_role,
        "dept_id": event.dept_id,
        "action": event.action,
        "resource": event.resource,
        "result": event.result,
        "payload": event.payload,
    }
    return json.dumps(envelope, default=str)


_FORBIDDEN_MARKERS = (
    "sk-ant-",
    "sk-proj-",
    "sk-live-",
    "sk-test-",
    "AIzaSy",
)


def _assert_clean(envelope: str) -> None:
    """Assert the serialised event has no unredacted credential marker."""

    for marker in _FORBIDDEN_MARKERS:
        # The marker is allowed to appear ONLY inside ``***REDACTED***``
        # markers (the redaction filter normalises every match to the
        # placeholder, so the raw prefix never survives).
        assert marker not in envelope, (
            f"unredacted credential marker {marker!r} in audit envelope"
        )


@pytest.mark.asyncio
async def test_create_emits_one_audit_event() -> None:
    service, _, _, _, audit = build_service()
    dto = await service.create(
        _ProviderCreateInput(
            provider_type="anthropic",
            name="Claude",
            model="claude-3-5-sonnet",
            context_length=200000,
            base_url=None,
            api_key="sk-ant-1234567890ABCDEFGHIJ",
            org_id=None,
        ),
        actor_id="admin-1",
    )
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "llm_provider_created"
    assert event.actor_id == "admin-1"
    assert event.actor_role == "admin"
    _assert_clean(_serialise(event))
    assert str(dto.id) in event.payload["provider_id"]


@pytest.mark.asyncio
async def test_update_emits_one_audit_event_per_call() -> None:
    service, _, _, _, audit = build_service()
    dto = await service.create(
        _ProviderCreateInput(
            provider_type="openai",
            name="openai",
            model="gpt-4o-mini",
            context_length=128000,
            base_url=None,
            api_key="sk-test-1234567890ABCDEFGH",
            org_id=None,
        ),
        actor_id="admin-1",
    )
    audit.events.clear()

    await service.update(
        dto.id, ProviderUpdate(name="renamed"), actor_id="admin-1"
    )
    assert len(audit.events) == 1
    assert audit.events[0].action == "llm_provider_updated"

    audit.events.clear()
    await service.update(
        dto.id,
        ProviderUpdate(api_key="sk-test-rotated1234567890ABCD"),
        actor_id="admin-1",
    )
    assert len(audit.events) == 1
    assert audit.events[0].action == "llm_provider_credentials_rotated"


@pytest.mark.asyncio
async def test_test_unsaved_emits_one_audit_event() -> None:
    service, _, _, _, audit = build_service()
    audit.events.clear()
    payload = UnsavedTestRequest(
        provider_type="openai",
        name="probe",
        model="gpt-4o-mini",
        context_length=128000,
        api_key="sk-test-1234567890ABCDEFGH",
    )
    await service.test_unsaved(payload, actor_id="admin-1")
    test_events = [
        e for e in audit.events if e.action == "llm_provider_test_unsaved"
    ]
    assert len(test_events) == 1
    _assert_clean(_serialise(test_events[0]))


@pytest.mark.asyncio
async def test_sink_failure_logs_warning_but_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing sink does not propagate ."""

    audit = RecordingAuditSink(raise_on={"llm_provider_created"})
    service, _, _, _, _ = build_service(audit=audit)

    with caplog.at_level(
        logging.WARNING, logger="src.llm_providers.service"
    ):
        dto = await service.create(
            _ProviderCreateInput(
                provider_type="anthropic",
                name="Claude",
                model="claude-3-5-sonnet",
                context_length=200000,
                base_url=None,
                api_key="sk-ant-1234567890ABCDEFGHIJ",
                org_id=None,
            ),
            actor_id="admin-1",
        )

    # The HTTP response would carry the created DTO; the service did
    # NOT raise just because the sink failed.
    assert dto is not None
    # And the failure surfaced as a structured WARNING log line.
    audit_failure_records = [
        r
        for r in caplog.records
        if "llm_provider_audit_emit_failed" in r.getMessage()
    ]
    assert audit_failure_records, (
        "expected llm_provider_audit_emit_failed warning on sink failure"
    )
