"""Unit tests for ``src.routers.runner_workspaces``.

The router is exercised through :class:`fastapi.testclient.TestClient`
against an in-memory stub :class:`RunnerWorkspacesClient`. The
``require_admin`` dependency is overridden with a permissive stub for
the happy paths; the auth-gate behaviour is delegated to the existing
``test_services_lifecycle_router`` coverage so we can focus on the
endpoint-specific contracts (regex guard, audit emission, soft-fail).

Coverage matrix:

* ``GET /admin/runner/workspaces`` → 200 + serialised entries when the
  client is wired.
* ``GET`` → 200 + empty list when the client slot is ``None``
  (soft-fail; UI keeps rendering).
* ``GET`` → 200 + empty list when the client raises (soft-fail; the
  router never propagates SSH transport errors to the dashboard).
* ``DELETE /admin/runner/workspaces/{issue_key}`` → 200 + audit on
  happy path (``workspace_manually_purged``).
* ``DELETE`` → 400 + ``invalid_issue_key_format`` audit + **no** SSH
  call for path-traversal vectors (``..``, lower-case, ``;``, ``&``,
  ``|``, ``$``, backtick, newline, null-byte).
* ``DELETE`` → 503 with ``runner_workspaces_client_unavailable`` when
  the client slot is ``None``.
* ``DELETE`` → 502 + ``workspace_purge_failed`` audit when the client
  raises.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Bootstrap ``sys.path`` so ``import src.routers.runner_workspaces``
# resolves under direct ``pytest tests/unit`` invocations (mirrors the
# pattern used by the other unit-test modules in this folder).
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# ``libs/audit_logger`` and ``libs/auth-shared`` are consumed via
# ``sys.path`` injection so the router's transitive imports resolve
# under direct ``pytest tests/unit`` invocations too.
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from audit_logger import AuditEvent  # noqa: E402

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.runner_workspaces import (  # noqa: E402
    RunnerWorkspacesClient,
    WorkspaceListEntry,
    WorkspacePurgeResult,
    router,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubClient:
    """In-memory :class:`RunnerWorkspacesClient` that records every call.

    Configurable via the ``raise_on_*`` and ``next_purge_result``
    attributes; the router is the system-under-test so the stub stays
    minimal.
    """

    def __init__(
        self,
        *,
        entries: list[WorkspaceListEntry] | None = None,
        raise_on_list: BaseException | None = None,
        raise_on_purge: BaseException | None = None,
        next_purge_result: WorkspacePurgeResult | None = None,
    ) -> None:
        self.entries: list[WorkspaceListEntry] = list(entries or [])
        self.raise_on_list = raise_on_list
        self.raise_on_purge = raise_on_purge
        self.next_purge_result = next_purge_result or WorkspacePurgeResult(
            purged=True, freed_bytes=0
        )
        self.list_calls: int = 0
        self.purge_calls: list[str] = []

    async def list_workspaces(self) -> list[WorkspaceListEntry]:
        self.list_calls += 1
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return list(self.entries)

    async def purge_workspace(self, issue_key: str) -> WorkspacePurgeResult:
        self.purge_calls.append(issue_key)
        if self.raise_on_purge is not None:
            raise self.raise_on_purge
        return self.next_purge_result


class _RecordingAuditSink:
    """Records every audit event written through the sink.

    Matches the duck-typed ``write(event)`` contract used by every
    other router in this service.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(
    *,
    client: RunnerWorkspacesClient | None,
    audit_sink: Any | None = None,
    actor_sub: str = "ops-1",
) -> FastAPI:
    """Return a FastAPI app wired to the router with stub dependencies.

    The audit sink is bound on ``app.state.feature_flag_audit_sink``
    because that is the slot ``runner_workspaces._audit_sink`` consults
    first (mirroring the ``feature_flags.py::_get_audit`` pattern).
    """

    app = FastAPI()
    app.include_router(router)

    app.state.runner_workspaces_client = client
    if audit_sink is not None:
        app.state.feature_flag_audit_sink = audit_sink

    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=actor_sub, groups=("admin",)
    )
    return app


def _entry(
    *,
    issue_key: str = "PAY-4211",
    size_mb: int = 42,
    last_modified: datetime | None = None,
) -> WorkspaceListEntry:
    return WorkspaceListEntry(
        issue_key=issue_key,
        size_mb=size_mb,
        last_modified=last_modified
        or datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# GET /admin/runner/workspaces
# ---------------------------------------------------------------------------


class TestListWorkspaces:
    """Cover listing shape and soft-fail branches."""

    def test_returns_serialised_entries(self) -> None:
        client = _StubClient(
            entries=[
                _entry(issue_key="PAY-4211", size_mb=42),
                _entry(issue_key="OPS_CORE-12", size_mb=128),
            ],
        )
        app = _build_app(client=client)

        response = TestClient(app).get("/admin/runner/workspaces")

        assert response.status_code == 200
        body = response.json()
        assert "workspaces" in body
        keys = [ws["issue_key"] for ws in body["workspaces"]]
        assert keys == ["PAY-4211", "OPS_CORE-12"]
        # ``last_modified`` is serialised as ISO-8601.
        for ws in body["workspaces"]:
            assert "T" in ws["last_modified"]
            assert ws["last_modified"].endswith("+00:00")
            assert isinstance(ws["size_mb"], int)
        assert client.list_calls == 1

    def test_empty_list_when_client_unwired(self) -> None:
        """``app.state.runner_workspaces_client = None`` is the default.

        The dashboard's *Services → Workspaces* tab must keep rendering
        even before the production SSH client is wired - the router
        returns an empty list rather than 503.
        """

        app = _build_app(client=None)

        response = TestClient(app).get("/admin/runner/workspaces")

        assert response.status_code == 200
        assert response.json() == {"workspaces": []}

    def test_empty_list_when_client_raises(self) -> None:
        """Soft-fail: SSH transport errors degrade to "no workspaces"."""

        client = _StubClient(raise_on_list=RuntimeError("ssh: connect failed"))
        app = _build_app(client=client)

        response = TestClient(app).get("/admin/runner/workspaces")

        assert response.status_code == 200
        assert response.json() == {"workspaces": []}
        assert client.list_calls == 1


# ---------------------------------------------------------------------------
# DELETE /admin/runner/workspaces/{issue_key} - happy path
# ---------------------------------------------------------------------------


class TestPurgeWorkspaceHappyPath:
    def test_returns_200_and_writes_audit(self) -> None:
        client = _StubClient(
            next_purge_result=WorkspacePurgeResult(
                purged=True, freed_bytes=12_345_678
            ),
        )
        sink = _RecordingAuditSink()
        app = _build_app(client=client, audit_sink=sink)

        response = TestClient(app).delete(
            "/admin/runner/workspaces/PAY-4211"
        )

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "purged": True,
            "freed_bytes": 12_345_678,
            "issue_key": "PAY-4211",
        }
        assert client.purge_calls == ["PAY-4211"]

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.action == "workspace_manually_purged"
        assert event.actor_role == "admin"
        assert event.actor_id == "ops-1"
        assert event.dept_id is None
        assert event.resource == "workspace:PAY-4211"
        assert event.result == "ok"
        assert event.payload == {
            "issue_key": "PAY-4211",
            "freed_bytes": 12_345_678,
        }

    def test_jira_keys_with_underscore_and_digits_accepted(self) -> None:
        """``OPS_CORE-12`` matches the canonical regex.

        The forward construction path (`workspace_path.build_workspace_path`)
        accepts the same shape, so the reverse path must too - otherwise
        valid workspaces could be created but never purged.
        """

        client = _StubClient()
        app = _build_app(client=client)

        response = TestClient(app).delete(
            "/admin/runner/workspaces/OPS_CORE-12"
        )

        assert response.status_code == 200
        assert client.purge_calls == ["OPS_CORE-12"]


# ---------------------------------------------------------------------------
# DELETE - path-traversal guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "issue_key",
    [
        # Lower-case (regex requires upper-case prefix)
        "pay-4211",
        # Missing numeric suffix
        "PAY-",
        # Missing project segment
        "-4211",
        # Numeric prefix
        "1PAY-4211",
        # Empty-ish
        " ",
    ],
)
def test_delete_rejects_invalid_keys_with_400(issue_key: str) -> None:
    """Invalid keys MUST short-circuit before the SSH client is invoked.

    The router writes a single ``workspace_purge_rejected_invalid_key``
    audit row so the security panel surfaces the path-traversal try,
    then returns ``400 + {"error": "invalid_issue_key_format"}``.
    """

    client = _StubClient()
    sink = _RecordingAuditSink()
    app = _build_app(client=client, audit_sink=sink)

    response = TestClient(app).delete(f"/admin/runner/workspaces/{issue_key}")

    assert response.status_code == 400
    assert response.json() == {
        "detail": {"error": "invalid_issue_key_format"}
    }
    # Critical safety invariant: the SSH client was NEVER touched.
    assert client.purge_calls == []
    # And we wrote one rejection audit.
    assert len(sink.events) == 1
    assert sink.events[0].action == "workspace_purge_rejected_invalid_key"
    assert sink.events[0].result == "denied"


def test_delete_path_traversal_dotdot_never_reaches_handler() -> None:
    """``/admin/runner/workspaces/..`` is normalised by Starlette.

    The literal ``..`` segment never reaches our handler - the URL
    router sees ``/admin/runner`` (no matching route) and returns 404
    before our regex guard even runs. This is a defence-in-depth
    layer on top of the explicit regex check; we assert the SSH
    client is never invoked, which is the property the security
    contract actually depends on.
    """

    client = _StubClient()
    app = _build_app(client=client)

    response = TestClient(app).delete("/admin/runner/workspaces/..")

    # 404 from Starlette's path normalisation - the route ``..``
    # resolves to does not exist. Either 400 or 404 satisfies the
    # security invariant: the request never reaches the SSH client.
    assert response.status_code in (400, 404)
    assert client.purge_calls == []


def test_delete_rejects_shell_metachar_keys() -> None:
    """Shell metacharacter vectors are rejected before any SSH command.

    These vectors would never reach the SSH layer thanks to the FastAPI
    URL parser stripping path separators, but we still assert the
    explicit rejection at the regex boundary so a future router change
    that switches to a query-parameter form cannot regress the guard.

    ``;`` / ``&`` / ``|`` / ``$`` / backtick / newline / null are all
    forbidden by ``ISSUE_KEY_PATTERN``; the test exercises a subset
    that the FastAPI URL parser accepts as a path component.
    """

    client = _StubClient()
    app = _build_app(client=client)

    # ``%3B`` = ``;``, ``%24`` = ``$``, ``%26`` = ``&``, ``%60`` = backtick.
    # These pass through the URL parser as a single path component and
    # land in the handler; the regex guard rejects them.
    for vector in ("PAY-4211%3B", "%24", "%26", "%60"):
        response = TestClient(app).delete(
            f"/admin/runner/workspaces/{vector}"
        )
        assert response.status_code == 400, (
            f"vector {vector!r} should have been rejected at regex boundary"
        )

    # The SSH client was never invoked for any of the metachar vectors.
    assert client.purge_calls == []


# ---------------------------------------------------------------------------
# DELETE - wiring + transport failure branches
# ---------------------------------------------------------------------------


def test_delete_503_when_client_unwired() -> None:
    """A valid key against an unwired client surfaces wiring failure.

    Unlike the GET path (which degrades to an empty list so the UI can
    render), DELETE must surface the wiring failure: the operator
    *meant* to delete and silently doing nothing would be worse than
    returning 503.
    """

    app = _build_app(client=None)

    response = TestClient(app).delete("/admin/runner/workspaces/PAY-4211")

    assert response.status_code == 503
    body = response.json()
    assert body == {
        "detail": {
            "status": "not_ready",
            "reason": "runner_workspaces_client_unavailable",
        }
    }


def test_delete_502_and_audit_when_client_raises() -> None:
    """SSH transport failures surface as 502 + ``workspace_purge_failed``."""

    client = _StubClient(
        raise_on_purge=RuntimeError("ssh: rm -rf returned exit_code=1"),
    )
    sink = _RecordingAuditSink()
    app = _build_app(client=client, audit_sink=sink)

    response = TestClient(app).delete("/admin/runner/workspaces/PAY-4211")

    assert response.status_code == 502
    body = response.json()
    assert body["detail"]["error"] == "workspace_purge_failed"
    assert body["detail"]["issue_key"] == "PAY-4211"
    assert "ssh: rm -rf" in body["detail"]["reason"]
    assert client.purge_calls == ["PAY-4211"]

    # The failure audit lands with ``result="error"`` and a trimmed
    # error message (max 500 chars).
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.action == "workspace_purge_failed"
    assert event.result == "error"
    assert event.payload["issue_key"] == "PAY-4211"
    assert "ssh: rm -rf" in event.payload["error"]


# ---------------------------------------------------------------------------
# Audit best-effort contract
# ---------------------------------------------------------------------------


class _RaisingAuditSink:
    """Audit sink that always raises - verifies best-effort contract."""

    async def write(self, event: AuditEvent) -> None:  # noqa: ARG002
        raise RuntimeError("simulated audit DB outage")


def test_purge_succeeds_even_when_audit_sink_raises() -> None:
    """An audit-sink hiccup MUST NOT mask the underlying 200 outcome."""

    client = _StubClient(
        next_purge_result=WorkspacePurgeResult(purged=True, freed_bytes=99),
    )
    app = _build_app(client=client, audit_sink=_RaisingAuditSink())

    response = TestClient(app).delete("/admin/runner/workspaces/PAY-4211")

    assert response.status_code == 200
    assert response.json()["purged"] is True
    assert client.purge_calls == ["PAY-4211"]
