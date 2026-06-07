"""Capability-denied webhook end-to-end integration test.



Scenario
--------

A Jira ``issue_created`` webhook arrives for a department whose bot
has *no* Atlassian credentials configured. The webhook handler in
``automation_service.webhooks_handlers`` runs through the canonical
chain (HMAC verify → loop guard → capability gate) and the capability
gate denies the request because the dept's derived capability set is
empty while the webhook-layer workflow type ``noop_test`` requires
``{"jira_read"}`` (see ``temporal_shared.WORKFLOW_TYPE_CAPABILITIES``).

The test asserts the key invariants for this denied path:

1. **Workflow start is blocked** - the injected workflow client's
 ``start_workflow`` is never invoked.
2. **HTTP response shape** - HTTP 202 with body
 ``{"status": "accepted", "decision": "denied",
 "missing": [...], "issue_key": ...}``.
3. **Jira bot comment** - a comment is posted on the issue whose body
 names the missing capability and is in Turkish for operators.
4. **Audit event** - exactly one event with
 ``action="capability_denied"``, ``result="denied"``, the resolved
 ``dept_id`` and a ``payload`` carrying the sorted ``missing``
 capability list.

Implementation notes
--------------------

The test drives the real FastAPI router via ``TestClient`` and
inject hand-written stubs for every collaborator (``DeptResolver``,
``VaultClient``, ``AuditLogger``, ``JiraCommenter``,
``SupportsStartWorkflow``). The pattern mirrors
``tests/property/test_webhook_predicates.py::TestWebhookDeptUnresolved``
so the integration test stays self-contained - no Postgres, no Vault
HTTP, no Temporal client. We compute a real HMAC-SHA256 signature
against a known per-dept secret stored in the stub vault so the
signature-verification leg succeeds and execution actually reaches
the capability gate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

# ---------------------------------------------------------------------------
# Make the in-tree ``services/automation-service/src`` importable so
# ``automation_service`` resolves without an editable install. Mirrors
# the bootstrap used by ``tests/property/test_webhook_predicates.py``;
# we duplicate it here because integration tests can be invoked
# standalone (``pytest tests/integration/test_capability_denied.py``)
# without first importing the property test module.
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "automation-service"
)
for _bootstrap_path in (_AUTOMATION_ROOT, _AUTOMATION_ROOT / "src"):
    _bs = str(_bootstrap_path)
    if str(_bootstrap_path) not in sys.path:
        sys.path.insert(0, _bs)

from fastapi.testclient import TestClient  # noqa: E402

from audit_logger import AuditEvent  # noqa: E402
from automation_service.app import create_app  # noqa: E402
from automation_service.webhooks_handlers import (  # noqa: E402
    BotRegistryEntry,
    WebhookContext,
)
from temporal_shared.capabilities import SupportsDepartment  # noqa: E402
from vault_client import VaultPath  # noqa: E402


# ---------------------------------------------------------------------------
# Lightweight stand-ins for the runtime collaborators
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubBotSection:
    """Empty bot section - no Jira / Bitbucket / Confluence credential.

 The capability resolver derives the empty frozenset for any dept
 whose ``bot.<svc>`` slots are all ``None``. That
 is exactly the precondition we need to make the capability gate
 deny ``noop_test`` (which requires ``jira_read``).
 """

    jira: object | None = None
    bitbucket: object | None = None
    confluence: object | None = None


@dataclass(frozen=True)
class _StubDept:
    """Minimal :class:`SupportsDepartment` stand-in with an ``id``.

 The webhook handler reads ``getattr(dept, "id", None)`` to obtain
 the dept_id used downstream (HMAC lookup, audit). Every other
 attribute is structural and forwarded to ``derive_capabilities``.
 """

    id: str
    web_search_enabled: bool = False
    bot: _StubBotSection = field(default_factory=_StubBotSection)


class _StubDeptResolver:
    """``DeptResolver`` stand-in backed by a static project_key → dept map."""

    def __init__(
        self,
        *,
        project_key_to_dept: Mapping[str, _StubDept],
        bot_registry: list[BotRegistryEntry] | None = None,
    ) -> None:
        self._mapping = dict(project_key_to_dept)
        self._registry = list(bot_registry or [])

    async def resolve_by_project_key(
        self, project_key: str
    ) -> SupportsDepartment | None:
        return self._mapping.get(project_key)

    async def list_bot_account_ids(self) -> list[BotRegistryEntry]:
        return list(self._registry)


class _StubVault:
    """In-memory ``VaultClient`` stand-in for a single webhook secret.

 Stores exactly one ``(VaultPath, payload)`` pair under
 ``vault:webhooks/jira/<dept_id>`` so HMAC verification succeeds
 when the request is signed with the matching secret. All other
 operations raise so accidental writes / rotations surface as
 test-double calls.
 """

    backend: str = "stub"

    def __init__(self, *, dept_id: str, secret: bytes) -> None:
        self._active_path = VaultPath.parse(f"vault:webhooks/jira/{dept_id}")
        self._secret = secret

    def read(self, path: VaultPath) -> dict[str, str]:
        if path == self._active_path:
            return {"secret": self._secret.decode("utf-8")}
        raise KeyError(str(path))

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete(self, path: VaultPath) -> None:  # pragma: no cover
        raise NotImplementedError

    def rotate_ssh_key(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def clear_previous_ssh_slot(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def rotate_webhook_secret(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@dataclass
class _RecordingJiraCommenter:
    """``JiraCommenter`` stand-in that records every comment posted."""

    comments: list[tuple[str, str, str]] = field(default_factory=list)
    """List of ``(dept_id, issue_key, body)`` triples."""

    async def post_comment(
        self, dept_id: str, issue_key: str, body: str
    ) -> None:
        self.comments.append((dept_id, issue_key, body))


class _RecordingAuditLogger:
    """Minimal ``AuditLogger`` shim that records each event in memory."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _SpyWorkflowClient:
    """``SupportsStartWorkflow`` stand-in that records every start call.

 The capability-denied path MUST short-circuit before reaching the
 workflow client. ``calls`` therefore stays empty for the
 test's happy path; if a regression sneaks in and the workflow
 starts despite the gate denying, ``calls`` non-emptiness flags it
 immediately.
 """

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        **kwargs: Any,
    ) -> Any:  # pragma: no cover - capability-denied path must not hit this
        self.calls.append(
            {
                "workflow": workflow,
                "args": list(args),
                "id": id,
                "task_queue": task_queue,
                "kwargs": dict(kwargs),
            }
        )

        @dataclass
        class _Handle:
            id: str

        return _Handle(id=id)


# ---------------------------------------------------------------------------
# Test fixture: build a fully wired app + signed webhook request
# ---------------------------------------------------------------------------


_DEPT_ID = "payments"
_PROJECT_KEY = "PAY"
_ISSUE_KEY = "PAY-4242"
_WEBHOOK_SECRET = b"test-only-webhook-secret-do-not-deploy"


def _build_signed_jira_payload() -> tuple[bytes, str]:
    """Return ``(raw_body, signature_header)`` for a well-formed payload.

 The payload's ``project.key`` resolves to ``payments`` via the
 stub resolver, and the actor is a real human (not a registered
 bot) so neither the dept_id-resolution nor the loop guard fires.
 """
    payload = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": _ISSUE_KEY,
            "fields": {
                "project": {"key": _PROJECT_KEY},
            },
        },
        "user": {"accountId": "human-engineer-001"},
    }
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    digest = hmac.new(_WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return body, f"sha256={digest}"


def _build_app() -> tuple[
    "TestClient",
    _RecordingAuditLogger,
    _RecordingJiraCommenter,
    _SpyWorkflowClient,
]:
    """Wire a FastAPI app whose dept has no Atlassian credentials.

 The dept resolves successfully (so HMAC verify can run), HMAC
 verification succeeds (matching the test's signed payload), and
 the loop guard passes (the actor is a human, not a bot). The
 capability gate then rejects ``noop_test`` because
 ``derive_capabilities`` returns an empty frozenset for a dept
 without any bot credentials.
 """
    audit = _RecordingAuditLogger()
    commenter = _RecordingJiraCommenter()
    workflow_client = _SpyWorkflowClient()

    dept = _StubDept(id=_DEPT_ID)
    ctx = WebhookContext(
        vault=_StubVault(dept_id=_DEPT_ID, secret=_WEBHOOK_SECRET),  # type: ignore[arg-type]
        dept_resolver=_StubDeptResolver(
            project_key_to_dept={_PROJECT_KEY: dept},
            bot_registry=[],  # no bots → loop guard cannot fire
        ),
        workflow_client=workflow_client,
        jira_commenter=commenter,
        audit_logger=audit,  # type: ignore[arg-type]
        env={},  # no SSH_HOST_*, no FIRECRAWL_ENABLED - all caps absent
        now_fn=lambda: datetime.now(timezone.utc),
    )

    app = create_app()
    app.state.webhook_v2 = ctx
    return TestClient(app), audit, commenter, workflow_client


# ---------------------------------------------------------------------------
# The integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_webhook_capability_denied_blocks_workflow_and_audits() -> None:
    """Jira webhook with insufficient capability is denied cleanly.

 Yetersiz capability ile gelen Jira webhook isteği için:

 * Workflow start çağrısı yapılmaz.
 * HTTP 202 döner ve body ``decision: "denied"`` içerir.
 * Jira'ya Türkçe bot yorumu yazılır ve eksik capability'leri
 isimlendirir.
 * Audit'e tek bir ``capability_denied`` (``result="denied"``)
 kaydı yazılır ve ``payload.missing`` eksik capability listesini
 içerir.
 """

    body, signature = _build_signed_jira_payload()
    client, audit, commenter, workflow_client = _build_app()

    try:
        resp = client.post(
            "/webhooks/jira/issue_created",
            content=body,
            headers={
                "X-Hub-Signature": signature,
                "Content-Type": "application/json",
            },
        )
    finally:
        client.close()

    # ---- (1) HTTP response shape -------------------------------------
    assert resp.status_code == 202, (
        f"expected HTTP 202 with decision=denied, got {resp.status_code} "
        f"with body={resp.text!r}"
    )
    response_body = resp.json()
    assert response_body["status"] == "accepted"
    assert response_body["decision"] == "denied"
    assert response_body["issue_key"] == _ISSUE_KEY
    missing_in_response = response_body["missing"]
    assert isinstance(missing_in_response, list) and missing_in_response, (
        f"expected non-empty missing list, got {missing_in_response!r}"
    )
    # noop_test only requires jira_read; with no Jira credential the
    # only missing capability is jira_read.
    assert missing_in_response == ["jira_read"], (
        f"expected missing=['jira_read'], got {missing_in_response!r}"
    )

    # ---- (2) Workflow start is blocked --------------------------------
    assert workflow_client.calls == [], (
        f"capability-denied path must not start a workflow; "
        f"got calls={workflow_client.calls!r}"
    )

    # ---- (3) Jira bot comment posted ---------------------------------
    assert len(commenter.comments) == 1, (
        f"expected exactly one bot comment on capability denial, "
        f"got {commenter.comments!r}"
    )
    comment_dept, comment_issue, comment_body = commenter.comments[0]
    assert comment_dept == _DEPT_ID
    assert comment_issue == _ISSUE_KEY
    # Turkish denial phrasing; the comment must
    # mention the missing capability so the operator knows what to
    # provision next.
    assert "jira_read" in comment_body, (
        f"comment body must name the missing capability; got {comment_body!r}"
    )
    assert "Otomasyon başlatılamadı" in comment_body, (
        f"comment body must use the Turkish denial phrasing; "
        f"got {comment_body!r}"
    )

    # ---- (4) Audit event written -------------------------------------
    capability_denied_events = [
        e for e in audit.events if e.action == "capability_denied"
    ]
    assert len(capability_denied_events) == 1, (
        f"expected exactly one capability_denied audit event, got "
        f"{[e.action for e in audit.events]!r}"
    )
    evt = capability_denied_events[0]
    assert evt.result == "denied"
    assert evt.dept_id == _DEPT_ID
    assert evt.actor_role == "system"
    assert evt.resource == "workflow:noop_test"
    assert evt.payload is not None
    # The handler stores ``missing`` as a sorted list so audit
    # consumers (and downstream tooling) get a canonical ordering.
    assert evt.payload.get("missing") == ["jira_read"]
    assert evt.payload.get("issue_key") == _ISSUE_KEY
    assert evt.payload.get("event_type") == "jira:issue_created"
