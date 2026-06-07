#
# Department CRUD hot-reload signal
#
"""Department CRUD hot-reload signal .
Department CRUD hot-reload signal**
For any successful CRUD operation against
``src.routers.departments.crud_router`` (``POST /api/v1/departments``,
``PATCH /api/v1/departments/{dept_id}``, ``DELETE
/api/v1/departments/{dept_id}``) the router SHALL emit **exactly one**
hot-reload signal carrying the dept id and the matching action label
(``dept_created`` / ``dept_updated`` / ``dept_decommissioned``) before
the response returns. This is the publisher-side guarantee that backs
the 10-second consumer-side propagation budget called out in -
the consumer cannot meet the SLA if the publisher never fires.
The complementary invariants on the failure / idempotent paths are
also covered:
* A 409 ``dept_id_conflict`` on POST MUST NOT emit a signal.
* A 404 on PATCH / DELETE MUST NOT emit a signal.
* A DELETE on an already-disabled dept (``status="already_disabled"``)
  MUST NOT emit a signal - the implementation early-returns without
  rewriting the file, so consumers do not need to be poked.
Strategy
--------
Hypothesis generates random short sequences of CRUD operations against
a transient ``departments.json`` rooted in ``tmp_path``. The module-level
``_DEPARTMENTS_CONFIG_PATH`` and ``_DEPARTMENTS_LOCK_PATH`` symbols on
``src.routers.departments`` are monkey-patched per example so the test
never touches the real platform config file.
The publisher is replaced with an :class:`_RecordingPublisher` instance
that captures every ``(dept_id, action)`` tuple. Each Hypothesis example
asserts:
1. After every successful POST / PATCH / DELETE the recorded list grew
   by exactly one tuple ``(dept_id, expected_action)``.
2. After every conflict / not-found / already-disabled response the
   list did not grow.
3. The total count of recorded signals across the whole sequence equals
   the number of mutations that landed on disk.
The actor is wired through a ``require_admin`` dependency override
mirroring the convention in ``test_workflow_control_audit_trail.py``."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors test_workflow_control_audit_trail.py)
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers import departments as departments_module  # noqa: E402
from src.routers.departments import crud_router  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Action labels emitted by ``_signal_hot_reload`` - kept in sync with the
#: module-level ``_ACTION_*`` constants on the router. Hard-coded here so
#: a typo in the router immediately fails the test instead of silently
#: agreeing with itself.
_ACTION_CREATED = "dept_created"
_ACTION_UPDATED = "dept_updated"
_ACTION_DECOMMISSIONED = "dept_decommissioned"

_ActionKind = Literal["create", "patch", "delete"]


# ---------------------------------------------------------------------------
# Recording publisher
# ---------------------------------------------------------------------------


@dataclass
class _RecordingPublisher:
    """Async fake for ``app.state.departments_reload_publisher``.

    Records every ``(dept_id, action)`` pair so the test can assert
    one-to-one correspondence with successful CRUD calls. The router
    short-circuits as soon as the publisher returns, so the HTTP fan-out
    fallback is never exercised when this fake is wired.
    """

    calls: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, dept_id: str, action: str) -> None:
        self.calls.append((dept_id, action))


# ---------------------------------------------------------------------------
# Minimal valid department payloads
# ---------------------------------------------------------------------------


def _minimal_dept(
    dept_id: str,
    *,
    display_name: str | None = None,
    mode: str = "active",
) -> dict[str, Any]:
    """Build a payload that passes ``departments.schema.json`` validation.
    Only the schema-required fields are populated; everything else is
    left to the schema defaults. We intentionally leave every bot
    ``account_id`` blank so the unique-account check cannot trip on
    the synthetic payloads; that branch is covered by dedicated tests.
    """

    return {
        "id": dept_id,
        "display_name": display_name or f"Dept {dept_id}",
        "jira_project_keys": [dept_id.upper().replace("-", "")[:9] or "DEFAULT"],
        "bot": {
            "jira": {
                "credential_ref": f"vault:atlassian/{dept_id}/jira",
                "account_id": "",
                "username": "",
            }
        },
        "budget_caps": {
            "weekly_usd_dept": 100.0,
            "weekly_usd_user": 10.0,
            "monthly_usd_dept": 400.0,
            "monthly_usd_user": 40.0,
        },
        "mode": mode,
    }


def _seed_doc(dept_ids: tuple[str, ...]) -> dict[str, Any]:
    """Construct a valid ``departments.json`` document seeded with depts."""

    return {
        "version": 1,
        "departments": [_minimal_dept(d) for d in dept_ids],
    }


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


_ACTOR_SUB = "admin-prop-test"


def _build_client(
    *,
    publisher: _RecordingPublisher,
) -> TestClient:
    """Return a TestClient with the ``crud_router`` mounted and wired."""

    app = FastAPI()
    app.include_router(crud_router)
    app.state.departments_reload_publisher = publisher
    # No audit / admin proxy: the router treats both as optional and the
    # property under test is the hot-reload signal, not the audit trail.
    app.state.dept_audit_sink = None
    app.state.admin_proxy = None
    app.state.http_client = None
    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=_ACTOR_SUB,
        groups=("admin",),
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Filesystem fixture per Hypothesis example
# ---------------------------------------------------------------------------


def _redirect_config_paths(
    monkeypatch: Any,
    tmp_root: Path,
    seed_ids: tuple[str, ...] = (),
) -> Path:
    """Point the router at a fresh ``departments.json`` under ``tmp_root``.

    The module-level constants are rebound rather than copied because the
    router reads them by name on every request, so monkey-patching the
    attribute is the lightest-touch redirection that exercises the real
    code path (lock acquisition, atomic write, schema validation).
    """

    cfg = tmp_root / "departments.json"
    lock = tmp_root / ".departments.json.lock"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(_seed_doc(seed_ids), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        departments_module, "_DEPARTMENTS_CONFIG_PATH", cfg, raising=True
    )
    monkeypatch.setattr(
        departments_module, "_DEPARTMENTS_LOCK_PATH", lock, raising=True
    )
    return cfg


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Department id slug - schema requires ``^[a-z][a-z0-9-]{1,30}$`` with
#: length in ``[2, 31]``. We narrow to a small alphabet and 2-8 chars so
#: collisions are likely (exercising the 409 path) but the slug is still
#: schema-valid.
_DEPT_ID_STRATEGY = st.from_regex(
    r"[a-z][a-z0-9]{1,7}",
    fullmatch=True,
)

#: Action kind for each step in the generated sequence.
_ACTION_STRATEGY: st.SearchStrategy[_ActionKind] = st.sampled_from(
    ["create", "patch", "delete"]
)


@st.composite
def _operation(draw: st.DrawFn) -> tuple[_ActionKind, str]:
    """Generate a single ``(action, dept_id)`` step."""

    return (draw(_ACTION_STRATEGY), draw(_DEPT_ID_STRATEGY))


_OPERATION_SEQUENCE = st.lists(_operation(), min_size=1, max_size=5)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _execute_step(
    client: TestClient,
    step: tuple[_ActionKind, str],
    cfg_path: Path,
) -> tuple[int, dict[str, Any], str | None]:
    """Drive one CRUD step and classify the response.

    Returns ``(status_code, body, expected_action_or_none)``. The third
    element is the hot-reload action label expected on the success path
    or ``None`` if the response was a conflict / 404 / idempotent
    no-op (where no signal is allowed).
    """

    action, dept_id = step

    # Snapshot the on-disk dept ids so we can predict whether the call
    # will succeed without depending on the router's own bookkeeping.
    doc = json.loads(cfg_path.read_text(encoding="utf-8"))
    existing = {d["id"]: d for d in doc.get("departments", [])}

    if action == "create":
        resp = client.post(
            "/api/v1/departments",
            json=_minimal_dept(dept_id),
        )
        if dept_id in existing:
            assert resp.status_code == 409, (
                f"create on existing id={dept_id!r} must 409; "
                f"got {resp.status_code} body={resp.text!r}"
            )
            return resp.status_code, resp.json(), None
        assert resp.status_code == 201, (
            f"create on fresh id={dept_id!r} must 201; "
            f"got {resp.status_code} body={resp.text!r}"
        )
        return resp.status_code, resp.json(), _ACTION_CREATED

    if action == "patch":
        resp = client.patch(
            f"/api/v1/departments/{dept_id}",
            json={"display_name": f"Updated {dept_id}"},
        )
        if dept_id not in existing:
            assert resp.status_code == 404, (
                f"patch on missing id={dept_id!r} must 404; "
                f"got {resp.status_code} body={resp.text!r}"
            )
            return resp.status_code, resp.json(), None
        assert resp.status_code == 200, (
            f"patch on existing id={dept_id!r} must 200; "
            f"got {resp.status_code} body={resp.text!r}"
        )
        return resp.status_code, resp.json(), _ACTION_UPDATED

    # action == "delete"
    resp = client.delete(f"/api/v1/departments/{dept_id}")
    if dept_id not in existing:
        assert resp.status_code == 404, (
            f"delete on missing id={dept_id!r} must 404; "
            f"got {resp.status_code} body={resp.text!r}"
        )
        return resp.status_code, resp.json(), None

    body = resp.json()
    if existing[dept_id].get("mode") == "disabled":
        # Idempotent path - already disabled before the call.
        assert resp.status_code == 200, (
            f"delete on already-disabled id={dept_id!r} must 200; "
            f"got {resp.status_code} body={resp.text!r}"
        )
        assert body.get("status") == "already_disabled", (
            f"delete on already-disabled id={dept_id!r} must report "
            f"status=already_disabled; got {body!r}"
        )
        return resp.status_code, body, None

    assert resp.status_code == 200, (
        f"delete on active id={dept_id!r} must 200; "
        f"got {resp.status_code} body={resp.text!r}"
    )
    assert body.get("status") == "decommissioned", (
        f"delete on active id={dept_id!r} must report "
        f"status=decommissioned; got {body!r}"
    )
    return resp.status_code, body, _ACTION_DECOMMISSIONED


# ---------------------------------------------------------------------------
# - every successful CRUD emits exactly one matching signal
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(operations=_OPERATION_SEQUENCE)
def test_every_successful_crud_signals_hot_reload_once(
    operations: list[tuple[_ActionKind, str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """- successful CRUD triggers exactly one signal each.
    For every randomly generated sequence of CRUD operations the
    publisher MUST receive one ``(dept_id, action)`` tuple per
    successful mutation, no tuples on conflict / not-found /
    idempotent paths, and the per-step ``action`` label MUST match
    the kind of mutation that landed on disk."""

    _redirect_config_paths(monkeypatch, tmp_path, seed_ids=())
    publisher = _RecordingPublisher()
    cfg_path = departments_module._DEPARTMENTS_CONFIG_PATH
    client = _build_client(publisher=publisher)

    expected_signals: list[tuple[str, str]] = []

    for step in operations:
        before = list(publisher.calls)
        _, _, expected_action = _execute_step(client, step, cfg_path)

        if expected_action is None:
            # Failure / idempotent path - no new signal allowed.
            assert publisher.calls == before, (
                f"step={step!r} returned a non-mutating response but the "
                f"publisher recorded a new signal (before={before!r}, "
                f"after={publisher.calls!r}); failed / no-op CRUD must "
                f"not emit hot-reload chatter."
            )
            continue

        # Successful mutation - exactly one new tuple, matching id+action.
        assert len(publisher.calls) == len(before) + 1, (
            f"step={step!r} expected exactly one new signal; "
            f"before={before!r}, after={publisher.calls!r}"
        )
        new_call = publisher.calls[-1]
        assert new_call == (step[1], expected_action), (
            f"step={step!r} expected signal {(step[1], expected_action)!r}, "
            f"got {new_call!r}"
        )
        expected_signals.append(new_call)

    assert publisher.calls == expected_signals, (
        f"final publisher tape diverged from the expected signal log: "
        f"calls={publisher.calls!r} expected={expected_signals!r}"
    )


# ---------------------------------------------------------------------------
# - POST conflict (409) does not signal hot-reload
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(dept_id=_DEPT_ID_STRATEGY)
def test_post_conflict_does_not_signal_hot_reload(
    dept_id: str,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """- duplicate POST must not signal hot-reload.
    When a ``POST /api/v1/departments`` returns 409
    ``dept_id_conflict``, no ``departments.json`` write happened, so
    the publisher MUST stay silent - anything else would burn the
    consumer-side reload budget on a no-op."""

    _redirect_config_paths(monkeypatch, tmp_path, seed_ids=(dept_id,))
    publisher = _RecordingPublisher()
    client = _build_client(publisher=publisher)

    # The seed already contains ``dept_id``; this POST must collide.
    resp = client.post(
        "/api/v1/departments",
        json=_minimal_dept(dept_id),
    )
    assert resp.status_code == 409, (
        f"expected 409 dept_id_conflict on duplicate POST; got "
        f"{resp.status_code} body={resp.text!r}"
    )
    assert publisher.calls == [], (
        f"409 conflict must not signal hot-reload; got {publisher.calls!r}"
    )


# ---------------------------------------------------------------------------
# - DELETE on already-disabled dept does not signal hot-reload
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(dept_id=_DEPT_ID_STRATEGY)
def test_delete_already_disabled_does_not_signal_hot_reload(
    dept_id: str,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """- DELETE on a disabled dept is a no-op signal-wise.
    When the dept is already at ``mode=disabled`` the router
    short-circuits with ``status="already_disabled"`` and skips the
    file rewrite. No consumer cache needs to refresh, so the publisher
    MUST stay silent on this path."""

    cfg = tmp_path / "departments.json"
    lock = tmp_path / ".departments.json.lock"
    doc = {
        "version": 1,
        "departments": [_minimal_dept(dept_id, mode="disabled")],
    }
    cfg.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        departments_module, "_DEPARTMENTS_CONFIG_PATH", cfg, raising=True
    )
    monkeypatch.setattr(
        departments_module, "_DEPARTMENTS_LOCK_PATH", lock, raising=True
    )

    publisher = _RecordingPublisher()
    client = _build_client(publisher=publisher)

    resp = client.delete(f"/api/v1/departments/{dept_id}")
    assert resp.status_code == 200, (
        f"DELETE on disabled dept must 200; got {resp.status_code} "
        f"body={resp.text!r}"
    )
    body = resp.json()
    assert body.get("status") == "already_disabled", (
        f"expected status=already_disabled; got {body!r}"
    )
    assert publisher.calls == [], (
        f"already_disabled DELETE must not signal hot-reload; got "
        f"{publisher.calls!r}"
    )


# ---------------------------------------------------------------------------
# - coverage of all three action labels
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(dept_id=_DEPT_ID_STRATEGY)
def test_full_lifecycle_covers_all_three_actions(
    dept_id: str,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """- create  patch  delete fires the three action labels.
    Walks one dept through its whole lifecycle and asserts the
    publisher tape is exactly ``[(id, dept_created), (id, dept_updated),
    (id, dept_decommissioned)]`` - confirming the three constants from
    the router are wired into the signal payload, in order."""

    _redirect_config_paths(monkeypatch, tmp_path, seed_ids=())
    publisher = _RecordingPublisher()
    client = _build_client(publisher=publisher)

    create = client.post(
        "/api/v1/departments", json=_minimal_dept(dept_id)
    )
    assert create.status_code == 201, create.text

    patch = client.patch(
        f"/api/v1/departments/{dept_id}",
        json={"display_name": f"Updated {dept_id}"},
    )
    assert patch.status_code == 200, patch.text

    delete = client.delete(f"/api/v1/departments/{dept_id}")
    assert delete.status_code == 200, delete.text

    assert publisher.calls == [
        (dept_id, _ACTION_CREATED),
        (dept_id, _ACTION_UPDATED),
        (dept_id, _ACTION_DECOMMISSIONED),
    ], (
        f"lifecycle signals diverged from the expected (create, update, "
        f"decommission) tape: {publisher.calls!r}"
    )
