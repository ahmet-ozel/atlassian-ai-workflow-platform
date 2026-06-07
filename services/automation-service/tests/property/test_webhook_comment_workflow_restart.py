"""Property test: Webhook comment decision determinism.

For any combination of ``(workflow_exists, issue_status,
assignee_account_id, dept_config)`` the handler's decision is
**deterministic** - the same inputs always produce the same outcome:

  * ``signal_forwarded``          - workflow exists, signal delivered.
  * ``workflow_restarted_from_comment`` - no workflow, status eligible,
                                    assignee is a dept bot.
  * ``comment_ignored_no_pending_workflow`` - no workflow AND (status
                                    not eligible OR assignee not a bot).

No real database or Temporal cluster is required; all I/O goes through
in-memory fakes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

#: ``automation-service/`` root so relative imports inside ``src/`` resolve.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

#: ``temporal-shared/src`` for ``temporal_shared.identifiers``.
_TEMPORAL_SHARED_SRC = (
    Path(__file__).resolve().parents[4] / "libs" / "temporal-shared" / "src"
)
if _TEMPORAL_SHARED_SRC.is_dir() and str(_TEMPORAL_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_TEMPORAL_SHARED_SRC))

from src.webhooks.jira import router as jira_router  # noqa: E402
from src.temporal_client import WorkflowNotFoundError  # noqa: E402
from temporal_shared.identifiers import automation_workflow_id_jira  # noqa: E402

# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_SECRET: bytes = b"test-comment-restart-secret-cafebabe"

_BOT_IDS: tuple[str, ...] = ("bot-jira-pay-001", "bot-jira-plat-002")
_NON_BOT_IDS: tuple[str, ...] = ("human-user-aaa", "human-user-bbb")

_ELIGIBLE_STATUSES: tuple[str, ...] = ("To Do", "Open")
_INELIGIBLE_STATUSES: tuple[str, ...] = ("In Progress", "Done", "Closed", "In Review")

_PROJECT_KEY = "PAY"
_DEPT_ID = "payment"

# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeStore:
    """Mutable state shared across all fake DB connections."""

    project_key_to_dept: dict[str, str]
    dept_with_jira_credential: set[str]
    processed_events: set[str]
    # dept_id  retrigger_eligible statuses (None = use default)
    dept_retrigger_config: dict[str, list[str] | None]


class _FakeConnection:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split()).lower()

        if "insert into automation.processed_events" in q:
            (event_hash, _ttl) = args
            if event_hash in self._store.processed_events:
                return None
            self._store.processed_events.add(event_hash)
            return {"event_hash": event_hash}

        if "select department_id" in q and "department_project_keys" in q:
            (project_key,) = args
            dept_id = self._store.project_key_to_dept.get(project_key)
            return {"department_id": dept_id} if dept_id else None

        if "from automation.department_bots" in q and "service = 'jira'" in q:
            (dept_id,) = args
            return {"?column?": 1} if dept_id in self._store.dept_with_jira_credential else None

        if "insert into automation.work_items" in q:
            return None  # comment_created never inserts work_items

        raise NotImplementedError(f"_FakeConnection.fetchrow: unsupported query: {query!r}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        raise NotImplementedError(f"_FakeConnection.fetch: unsupported: {query!r}")

    async def fetchval(self, query: str, *args: Any) -> Any:
        raise NotImplementedError(f"_FakeConnection.fetchval: unsupported: {query!r}")

    async def execute(self, query: str, *args: Any) -> str:
        raise NotImplementedError(f"_FakeConnection.execute: unsupported: {query!r}")


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakePool:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(_FakeConnection(self._store))

    # departments config_json lookup used by _retrigger_eligible_statuses
    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split()).lower()
        if "select config_json" in q and "automation.departments" in q:
            (dept_id,) = args
            cfg = self._store.dept_retrigger_config.get(dept_id)
            if cfg is None:
                return None
            config_json = {"task_status_mapping": {"retrigger_eligible": cfg}}
            return {"config_json": config_json}
        raise NotImplementedError(f"_FakePool.fetchrow: unsupported: {query!r}")


# ---------------------------------------------------------------------------
# Fake asyncpg pool that also supports the context-manager acquire pattern
# AND direct fetchrow (used by _retrigger_eligible_statuses via db.acquire)
# ---------------------------------------------------------------------------

class _FakePoolWithDeptConfig(_FakePool):
    """Extends _FakePool so that connections also handle departments query."""

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(_FakeConnectionWithDeptConfig(self._store))


class _FakeConnectionWithDeptConfig(_FakeConnection):
    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split()).lower()
        if "select config_json" in q and "automation.departments" in q:
            (dept_id,) = args
            cfg = self._store.dept_retrigger_config.get(dept_id)
            if cfg is None:
                return None
            config_json = {"task_status_mapping": {"retrigger_eligible": cfg}}
            return {"config_json": config_json}
        return await super().fetchrow(query, *args)

# ---------------------------------------------------------------------------
# Fake Temporal client
# ---------------------------------------------------------------------------


@dataclass
class _SignalWithStartCall:
    workflow_type: str
    workflow_id: str
    task_queue: str
    signal_name: str
    signal_payload: Any
    args: tuple[Any, ...]


class _FakeTemporalClient:
    """Records calls; raises WorkflowNotFoundError on signal_workflow when
    ``workflow_exists=False``."""

    def __init__(self, *, workflow_exists: bool) -> None:
        self.workflow_exists = workflow_exists
        self.signal_calls: list[tuple[str, str, Any]] = []
        self.signal_with_start_calls: list[_SignalWithStartCall] = []
        self.start_calls: list[Any] = []

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any = None,
    ) -> None:
        if not self.workflow_exists:
            raise WorkflowNotFoundError(workflow_id=workflow_id)
        self.signal_calls.append((workflow_id, signal_name, payload))

    async def signal_with_start(
        self,
        workflow_type: str,
        workflow_id: str,
        *,
        task_queue: str,
        signal_name: str,
        signal_payload: Any = None,
        args: Any = (),
    ) -> None:
        self.signal_with_start_calls.append(
            _SignalWithStartCall(
                workflow_type=workflow_type,
                workflow_id=workflow_id,
                task_queue=task_queue,
                signal_name=signal_name,
                signal_payload=signal_payload,
                args=tuple(args),
            )
        )

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        self.start_calls.append((args, kwargs))


# ---------------------------------------------------------------------------
# Fake CredentialResolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeBotRow:
    service: str
    account_id: str | None
    department_id: str


class _FakeCredentialResolver:
    def __init__(self, bots: list[_FakeBotRow]) -> None:
        self._bots = bots

    async def list_dept_bots(self) -> list[_FakeBotRow]:
        return list(self._bots)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(
    *,
    pool: _FakePoolWithDeptConfig,
    temporal: _FakeTemporalClient,
    creds: _FakeCredentialResolver,
    issue_status: str,
    assignee_id: str | None,
    secret: bytes = _TEST_SECRET,
) -> FastAPI:
    """Wire the Jira router to fakes and inject a jira_fetch_issue stub."""

    app = FastAPI()
    app.include_router(jira_router, prefix="/webhooks")
    app.state.db = pool
    app.state.temporal = temporal
    app.state.creds = creds
    app.state.jira_webhook_secret = secret

    async def _fetch_issue(ik: str, dept_id: str) -> tuple[str | None, str | None]:
        return issue_status, assignee_id

    app.state.jira_fetch_issue = _fetch_issue
    return app


# ---------------------------------------------------------------------------
# Request helper
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: bytes = _TEST_SECRET) -> str:
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _post(app: FastAPI, body: bytes) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Hub-Signature": _sign(body),
                "Content-Type": "application/json",
            },
        )


def _comment_payload(
    issue_key: str,
    actor_id: str,
    comment_text: str,
) -> bytes:
    """Build a minimal ``jira:comment_created`` webhook payload."""
    return json.dumps(
        {
            "webhookEvent": "jira:comment_created",
            "user": {"accountId": actor_id},
            "issue": {
                "key": issue_key,
                "fields": {
                    "project": {"key": issue_key.rsplit("-", 1)[0]},
                },
            },
            "comment": {
                "body": comment_text,
                "author": {"accountId": actor_id},
            },
        }
    ).encode()

# ---------------------------------------------------------------------------
# Fresh store factory
# ---------------------------------------------------------------------------


def _fresh_store(
    *,
    retrigger_statuses: list[str] | None = None,
) -> _FakeStore:
    """Return a clean in-memory store for one test run.

    ``retrigger_statuses=None`` means the dept has no custom config
    (handler falls back to the default ``["To Do", "Open"]``).
    """
    return _FakeStore(
        project_key_to_dept={_PROJECT_KEY: _DEPT_ID},
        dept_with_jira_credential={_DEPT_ID},
        processed_events=set(),
        dept_retrigger_config={_DEPT_ID: retrigger_statuses},
    )


def _make_creds(*, bot_ids: tuple[str, ...] = _BOT_IDS) -> _FakeCredentialResolver:
    return _FakeCredentialResolver(
        [
            _FakeBotRow(service="jira", account_id=bid, department_id=_DEPT_ID)
            for bid in bot_ids
        ]
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = st.integers(min_value=1, max_value=9999)
_COMMENT_TEXT = st.text(min_size=0, max_size=200)
_ACTOR_ID = st.sampled_from(_NON_BOT_IDS)  # actor is never a bot (loop-guard)


@st.composite
def _scenario_workflow_exists(draw: st.DrawFn) -> dict[str, Any]:
    """Scenario A: workflow exists  signal_forwarded."""
    issue_num = draw(_ISSUE_NUMBER)
    issue_key = f"{_PROJECT_KEY}-{issue_num}"
    actor_id = draw(_ACTOR_ID)
    comment_text = draw(_COMMENT_TEXT)
    return {
        "issue_key": issue_key,
        "actor_id": actor_id,
        "comment_text": comment_text,
        "workflow_exists": True,
        "issue_status": draw(st.sampled_from(_ELIGIBLE_STATUSES + _INELIGIBLE_STATUSES)),
        "assignee_id": draw(st.sampled_from(_BOT_IDS + _NON_BOT_IDS)),
        "retrigger_statuses": None,
        "expected_outcome": "signal_forwarded",
    }


@st.composite
def _scenario_restart(draw: st.DrawFn) -> dict[str, Any]:
    """Scenario B: no workflow + eligible status + bot assignee  restarted."""
    issue_num = draw(_ISSUE_NUMBER)
    issue_key = f"{_PROJECT_KEY}-{issue_num}"
    actor_id = draw(_ACTOR_ID)
    comment_text = draw(_COMMENT_TEXT)
    # Use either default eligible statuses or a custom set
    use_custom = draw(st.booleans())
    if use_custom:
        custom = draw(
            st.lists(
                st.text(min_size=1, max_size=20),
                min_size=1,
                max_size=4,
            )
        )
        issue_status = draw(st.sampled_from(custom))
        retrigger_statuses: list[str] | None = custom
    else:
        issue_status = draw(st.sampled_from(list(_ELIGIBLE_STATUSES)))
        retrigger_statuses = None
    assignee_id = draw(st.sampled_from(_BOT_IDS))
    return {
        "issue_key": issue_key,
        "actor_id": actor_id,
        "comment_text": comment_text,
        "workflow_exists": False,
        "issue_status": issue_status,
        "assignee_id": assignee_id,
        "retrigger_statuses": retrigger_statuses,
        "expected_outcome": "restarted",
    }

@st.composite
def _scenario_ignored(draw: st.DrawFn) -> dict[str, Any]:
    """Scenario C: no workflow + (ineligible status OR non-bot assignee)  ignored."""
    issue_num = draw(_ISSUE_NUMBER)
    issue_key = f"{_PROJECT_KEY}-{issue_num}"
    actor_id = draw(_ACTOR_ID)
    comment_text = draw(_COMMENT_TEXT)

    # Two sub-cases: ineligible status (with bot assignee) OR non-bot assignee
    sub = draw(st.sampled_from(["ineligible_status", "non_bot_assignee"]))
    if sub == "ineligible_status":
        issue_status = draw(st.sampled_from(_INELIGIBLE_STATUSES))
        assignee_id: str | None = draw(st.sampled_from(_BOT_IDS))
    else:
        issue_status = draw(st.sampled_from(_ELIGIBLE_STATUSES))
        assignee_id = draw(st.sampled_from(_NON_BOT_IDS + (None,)))

    return {
        "issue_key": issue_key,
        "actor_id": actor_id,
        "comment_text": comment_text,
        "workflow_exists": False,
        "issue_status": issue_status,
        "assignee_id": assignee_id,
        "retrigger_statuses": None,
        "expected_outcome": "ignored",
    }


# Combined strategy: pick one of the three scenarios uniformly
_any_scenario = st.one_of(
    _scenario_workflow_exists(),
    _scenario_restart(),
    _scenario_ignored(),
)

# ---------------------------------------------------------------------------
# Decision determinism
# ---------------------------------------------------------------------------


class TestWebhookCommentDecisionDeterminism:
    """Webhook comment decision determinism.

    For any ``(workflow_exists, issue_status, assignee_account_id,
    dept_config)`` combination the handler always produces the same
    outcome:

    * ``signal_forwarded``                   - workflow exists.
    * ``restarted``                          - no workflow, eligible
                                               status, bot assignee.
    * ``ignored`` (no_pending_workflow)      - no workflow, ineligible
                                               status or non-bot assignee.
    """

    @_PROFILE
    @given(_any_scenario)
    @pytest.mark.asyncio
    async def test_decision_is_deterministic(self, scenario: dict[str, Any]) -> None:
        """Same inputs  same outcome on every invocation."""
        issue_key: str = scenario["issue_key"]
        actor_id: str = scenario["actor_id"]
        comment_text: str = scenario["comment_text"]
        workflow_exists: bool = scenario["workflow_exists"]
        issue_status: str = scenario["issue_status"]
        assignee_id: str | None = scenario["assignee_id"]
        retrigger_statuses: list[str] | None = scenario["retrigger_statuses"]
        expected_outcome: str = scenario["expected_outcome"]

        store = _fresh_store(retrigger_statuses=retrigger_statuses)
        pool = _FakePoolWithDeptConfig(store)
        temporal = _FakeTemporalClient(workflow_exists=workflow_exists)
        creds = _make_creds()
        app = _build_app(
            pool=pool,
            temporal=temporal,
            creds=creds,
            issue_status=issue_status,
            assignee_id=assignee_id,
        )

        body = _comment_payload(issue_key, actor_id, comment_text)
        resp = await _post(app, body)

        assert resp.status_code == 200, resp.text
        body_json = resp.json()
        actual_status = body_json.get("status")

        assert actual_status == expected_outcome, (
            f"Expected outcome={expected_outcome!r} but got {actual_status!r}. "
            f"scenario={scenario!r}, response={body_json!r}"
        )

    @_PROFILE
    @given(_any_scenario)
    @pytest.mark.asyncio
    async def test_same_inputs_same_outcome_twice(self, scenario: dict[str, Any]) -> None:
        """Running the handler twice with identical inputs yields the same status.

        The second call may be a replay (duplicate) for the non-comment
        path, but for comment_created the replay guard fires on the
        second delivery - the important invariant is that the *first*
        call's outcome is stable.
        """
        issue_key: str = scenario["issue_key"]
        actor_id: str = scenario["actor_id"]
        comment_text: str = scenario["comment_text"]
        workflow_exists: bool = scenario["workflow_exists"]
        issue_status: str = scenario["issue_status"]
        assignee_id: str | None = scenario["assignee_id"]
        retrigger_statuses: list[str] | None = scenario["retrigger_statuses"]
        expected_outcome: str = scenario["expected_outcome"]

        body = _comment_payload(issue_key, actor_id, comment_text)

        # First run
        store1 = _fresh_store(retrigger_statuses=retrigger_statuses)
        pool1 = _FakePoolWithDeptConfig(store1)
        temporal1 = _FakeTemporalClient(workflow_exists=workflow_exists)
        creds1 = _make_creds()
        app1 = _build_app(
            pool=pool1, temporal=temporal1, creds=creds1,
            issue_status=issue_status, assignee_id=assignee_id,
        )
        resp1 = await _post(app1, body)

        # Second run (fresh store - same inputs, independent state)
        store2 = _fresh_store(retrigger_statuses=retrigger_statuses)
        pool2 = _FakePoolWithDeptConfig(store2)
        temporal2 = _FakeTemporalClient(workflow_exists=workflow_exists)
        creds2 = _make_creds()
        app2 = _build_app(
            pool=pool2, temporal=temporal2, creds=creds2,
            issue_status=issue_status, assignee_id=assignee_id,
        )
        resp2 = await _post(app2, body)

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json().get("status") == resp2.json().get("status") == expected_outcome, (
            f"Non-determinism detected: run1={resp1.json()!r}, run2={resp2.json()!r}, "
            f"scenario={scenario!r}"
        )


# ---------------------------------------------------------------------------
# Temporal call invariants
# ---------------------------------------------------------------------------


class TestWebhookCommentTemporalCallInvariants:
    """Verify the correct Temporal method is called for each outcome."""

    @_PROFILE
    @given(_scenario_workflow_exists())
    @pytest.mark.asyncio
    async def test_signal_forwarded_calls_signal_workflow(
        self, scenario: dict[str, Any]
    ) -> None:
        """Existing workflow  ``signal_workflow`` called."""
        store = _fresh_store()
        pool = _FakePoolWithDeptConfig(store)
        temporal = _FakeTemporalClient(workflow_exists=True)
        creds = _make_creds()
        app = _build_app(
            pool=pool, temporal=temporal, creds=creds,
            issue_status=scenario["issue_status"],
            assignee_id=scenario["assignee_id"],
        )
        body = _comment_payload(
            scenario["issue_key"], scenario["actor_id"], scenario["comment_text"]
        )
        resp = await _post(app, body)

        assert resp.json().get("status") == "signal_forwarded"
        assert len(temporal.signal_calls) == 1
        assert temporal.signal_with_start_calls == []
        assert temporal.start_calls == []

    @_PROFILE
    @given(_scenario_restart())
    @pytest.mark.asyncio
    async def test_restart_calls_signal_with_start(
        self, scenario: dict[str, Any]
    ) -> None:
        """Restart  ``signal_with_start`` called."""
        store = _fresh_store(retrigger_statuses=scenario["retrigger_statuses"])
        pool = _FakePoolWithDeptConfig(store)
        temporal = _FakeTemporalClient(workflow_exists=False)
        creds = _make_creds()
        app = _build_app(
            pool=pool, temporal=temporal, creds=creds,
            issue_status=scenario["issue_status"],
            assignee_id=scenario["assignee_id"],
        )
        body = _comment_payload(
            scenario["issue_key"], scenario["actor_id"], scenario["comment_text"]
        )
        resp = await _post(app, body)

        assert resp.json().get("status") == "restarted", resp.json()
        assert len(temporal.signal_with_start_calls) == 1
        call = temporal.signal_with_start_calls[0]
        assert call.signal_name == "new_comment"
        assert call.workflow_type == "AutomationWorkflow"
        assert temporal.signal_calls == []

    @_PROFILE
    @given(_scenario_ignored())
    @pytest.mark.asyncio
    async def test_ignored_makes_no_temporal_calls(
        self, scenario: dict[str, Any]
    ) -> None:
        """Ignored outcome  no Temporal calls."""
        store = _fresh_store()
        pool = _FakePoolWithDeptConfig(store)
        temporal = _FakeTemporalClient(workflow_exists=False)
        creds = _make_creds()
        app = _build_app(
            pool=pool, temporal=temporal, creds=creds,
            issue_status=scenario["issue_status"],
            assignee_id=scenario["assignee_id"],
        )
        body = _comment_payload(
            scenario["issue_key"], scenario["actor_id"], scenario["comment_text"]
        )
        resp = await _post(app, body)

        assert resp.json().get("status") == "ignored"
        assert temporal.signal_calls == []
        assert temporal.signal_with_start_calls == []
        assert temporal.start_calls == []
