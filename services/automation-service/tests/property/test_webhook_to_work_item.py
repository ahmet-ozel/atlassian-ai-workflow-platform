"""Property tests for the Jira webhook  ``work_items`` post-condition.

Webhook  ``work_items`` post-condition:

For any well-formed Jira webhook payload ``p`` whose six pre-conditions
hold simultaneously -

  1. valid HMAC signature against the dept's secret,
  2. payload hash NOT already in ``automation.processed_events``,
  3. actor is NOT in the bot registry,
  4. event type requires a workflow start (``issue_created``,
     ``issue_assigned``, or ``issue_updated`` with assigneebot change),
  5. resolved/changed assignee matches a registered bot,
  6. capability gate Phase 1 passes (department exists for the
     ``project_key`` AND has a Jira bot credential),

- after the handler returns ``200 status:accepted``, *all* of the
following hold:

  • ``automation.processed_events`` has *exactly one* row whose
    ``event_hash`` equals ``sha256(canonical_json(p))``.
  • ``automation.work_items`` has *exactly one* row with
    ``workflow_id == automation_workflow_id_jira(p.issue.key)``,
    ``status == 'pending'``, the resolved ``department_id``, and the
    issue key.
  • ``temporal.start_workflow`` has been called *exactly once* with the
    same ``workflow_id`` and ``workflow_type == "AutomationWorkflow"``.
  • The HTTP response is 200 with body
    ``{"status": "accepted", "workflow_id": ...}``.

If *any* of the six pre-conditions fails, the handler short-circuits to
exactly one of ``401 unauthorized`` / ``200 duplicate`` /
``200 loop_guard`` / ``200 ignored`` / ``200 not_bot_assignee`` /
``200 missing_capability`` / ``400 bad_request`` and *no* row is
inserted into ``work_items``.

Both legs of the disjunction are tested with the same Hypothesis
composite generator, parameterised by which (if any) pre-condition is
intentionally violated. All Postgres and Temporal interactions go
through in-memory fakes - no real database or Temporal cluster is
required.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors the sibling property tests)
# ---------------------------------------------------------------------------

#: ``automation-service/`` so that ``import src.webhooks.jira`` resolves
#: through the package's relative imports (``..decision`` etc.).
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

#: ``temporal-shared/src`` for ``temporal_shared.identifiers``.
_TEMPORAL_SHARED_SRC = (
    Path(__file__).resolve().parents[4] / "libs" / "temporal-shared" / "src"
)
if (
    _TEMPORAL_SHARED_SRC.is_dir()
    and str(_TEMPORAL_SHARED_SRC) not in sys.path
):
    sys.path.insert(0, str(_TEMPORAL_SHARED_SRC))

# Imports of the system under test.
from src.webhooks.jira import router as jira_router  # noqa: E402

from temporal_shared.identifiers import (  # noqa: E402
    automation_workflow_id_jira,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

#: A modest example budget. Each example boots an ASGI request through
#: the full guard chain, so we stay below 60 examples to keep the suite
#: well under the property-suite SLA.
_PROFILE = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)


# ===========================================================================
# In-memory fakes
# ===========================================================================


# --- asyncpg pool -----------------------------------------------------------


@dataclass
class _ProcessedEventRow:
    event_hash: str


@dataclass
class _WorkItemRow:
    id: int
    workflow_id: str
    department_id: str
    issue_key: str
    status: str


class _FakeConnection:
    """Stand-in for an asyncpg ``Connection``.

    Implements only the SQL shapes the Jira webhook handler issues:

    * ``INSERT INTO automation.processed_events (event_hash, expires_at)
       VALUES ($1, now() + $2::interval)
       ON CONFLICT (event_hash) DO NOTHING
       RETURNING event_hash``
    * ``SELECT department_id FROM automation.department_project_keys
       WHERE project_key = $1``
    * ``SELECT 1 FROM automation.department_bots
       WHERE department_id = $1 AND service = 'jira'``
    * ``INSERT INTO automation.work_items
       (workflow_id, department_id, issue_key, status)
       VALUES ($1, $2, $3, 'pending')
       ON CONFLICT (workflow_id) DO NOTHING
       RETURNING id``

    Anything else raises ``NotImplementedError`` so accidental misuse is
    loud.
    """

    def __init__(self, store: "_FakeStore") -> None:
        self._store = store

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        normalised = " ".join(query.split()).lower()

        # processed_events insert
        if "insert into automation.processed_events" in normalised:
            (event_hash, _ttl) = args
            if event_hash in self._store.processed_events:
                return None
            self._store.processed_events.add(event_hash)
            return {"event_hash": event_hash}

        # department_project_keys lookup
        if (
            "select department_id" in normalised
            and "from automation.department_project_keys" in normalised
        ):
            (project_key,) = args
            dept_id = self._store.project_key_to_dept.get(project_key)
            if dept_id is None:
                return None
            return {"department_id": dept_id}

        # has_jira_credential probe
        if (
            "from automation.department_bots" in normalised
            and "service = 'jira'" in normalised
        ):
            (dept_id,) = args
            if dept_id in self._store.dept_with_jira_credential:
                return {"?column?": 1}
            return None

        # work_items insert
        if "insert into automation.work_items" in normalised:
            workflow_id, department_id, issue_key = args
            existing = next(
                (
                    w
                    for w in self._store.work_items
                    if w.workflow_id == workflow_id
                ),
                None,
            )
            if existing is not None:
                return None
            new_row = _WorkItemRow(
                id=len(self._store.work_items) + 1,
                workflow_id=workflow_id,
                department_id=department_id,
                issue_key=issue_key,
                status="pending",
            )
            self._store.work_items.append(new_row)
            return {"id": new_row.id}

        raise NotImplementedError(
            f"_FakeConnection received unsupported query: {query!r}"
        )

    async def fetch(
        self, query: str, *args: Any
    ) -> list[dict[str, Any]]:  # pragma: no cover - unused by jira.py
        raise NotImplementedError(
            f"_FakeConnection.fetch not stubbed for: {query!r}"
        )

    async def execute(
        self, query: str, *args: Any
    ) -> str:  # pragma: no cover - unused by jira.py
        raise NotImplementedError(
            f"_FakeConnection.execute not stubbed for: {query!r}"
        )


class _FakeAcquireContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


@dataclass
class _FakeStore:
    """Mutable database state shared by every connection acquired."""

    #: ``project_key``  ``department_id`` (for
    #: ``automation.department_project_keys``). Missing keys  no row.
    project_key_to_dept: dict[str, str]

    #: Set of ``department_id`` values that have a Jira credential row
    #: in ``automation.department_bots``.
    dept_with_jira_credential: set[str]

    #: ``automation.processed_events`` keyed by ``event_hash``.
    processed_events: set[str]

    #: ``automation.work_items`` rows in insertion order.
    work_items: list[_WorkItemRow]


class _FakePool:
    """In-memory asyncpg ``Pool`` substitute for the Jira webhook tests."""

    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(_FakeConnection(self._store))

    @property
    def store(self) -> _FakeStore:
        return self._store


# --- Temporal client --------------------------------------------------------


@dataclass
class _StartWorkflowCall:
    workflow_type: str
    workflow_id: str
    task_queue: str
    args: tuple[Any, ...]


class _FakeTemporalClient:
    """Records ``start_workflow`` / ``signal_workflow`` invocations."""

    def __init__(self) -> None:
        self.start_calls: list[_StartWorkflowCall] = []
        self.signal_calls: list[tuple[str, str, Any]] = []

    async def start_workflow(
        self,
        *,
        workflow_type: str,
        workflow_id: str,
        task_queue: str,
        args: list[Any] | tuple[Any, ...] = (),
    ) -> None:
        self.start_calls.append(
            _StartWorkflowCall(
                workflow_type=workflow_type,
                workflow_id=workflow_id,
                task_queue=task_queue,
                args=tuple(args),
            )
        )

    async def signal_workflow(
        self,
        *,
        workflow_id: str,
        signal_name: str,
        payload: Any = None,
    ) -> None:  # pragma: no cover - not exercised by these tests
        self.signal_calls.append((workflow_id, signal_name, payload))


# --- CredentialResolver -----------------------------------------------------


@dataclass(frozen=True)
class _FakeBotRow:
    """Minimal stand-in for ``DeptBotRow`` (only fields the handler reads)."""

    service: str
    account_id: str | None


class _FakeCredentialResolver:
    """Fixed list of bot rows; mirrors ``CredentialResolver.list_dept_bots``."""

    def __init__(self, bots: list[_FakeBotRow]) -> None:
        self._bots = bots

    async def list_dept_bots(self) -> list[_FakeBotRow]:
        return list(self._bots)


# ===========================================================================
# Test app factory
# ===========================================================================


#: Fixed test secret used by every example. Hypothesis flips between
#: signing with this secret (valid HMAC) and a different secret
#: (invalid HMAC).
_TEST_SECRET: bytes = b"test-jira-webhook-secret-deadbeef"

#: Bot account IDs the test registry knows about. The handler treats
#: anything in this set as a bot for ``is_self_actor`` and
#: ``is_bot_assignee`` checks.
_BOT_ACCOUNT_IDS: tuple[str, ...] = (
    "bot-jira-payment-001",
    "bot-jira-platform-002",
)

#: Project keys with a configured department mapping AND a Jira
#: credential - i.e. capability gate Phase 1 passes for these.
_PROJECT_KEYS_WITH_CAPABILITY: dict[str, str] = {
    "PAY": "payment",
    "PLAT": "platform",
}

#: Project keys that have a department mapping but NO Jira credential -
#: capability gate denies. (For these tests we don't strictly need this
#: case to be distinct from "no mapping at all", but exercising both
#: paths gives Hypothesis more shrinking surface.)
_PROJECT_KEYS_DEPT_NO_CRED: dict[str, str] = {
    "ORPH": "orphan-dept",
}


def _build_test_app(
    *,
    pool: _FakePool,
    temporal: _FakeTemporalClient,
    creds: _FakeCredentialResolver,
    secret: bytes = _TEST_SECRET,
) -> FastAPI:
    """Assemble a FastAPI app that wires the Jira router to the fakes."""

    app = FastAPI()
    app.include_router(jira_router, prefix="/webhooks")
    app.state.db = pool
    app.state.temporal = temporal
    app.state.creds = creds
    app.state.jira_webhook_secret = secret
    # Best-effort ack callable is optional; skip it so the handler's
    # ``app.state.jira_ack_comment`` lookup returns ``None``.
    return app


def _fresh_store() -> _FakeStore:
    return _FakeStore(
        project_key_to_dept={
            **_PROJECT_KEYS_WITH_CAPABILITY,
            **_PROJECT_KEYS_DEPT_NO_CRED,
        },
        dept_with_jira_credential=set(_PROJECT_KEYS_WITH_CAPABILITY.values()),
        processed_events=set(),
        work_items=[],
    )


# ===========================================================================
# Hypothesis strategies
# ===========================================================================


#: Issue numeric suffix ≥ 1 with no leading zero (matches
#: ``temporal_shared.identifiers._ISSUE_KEY_RE``).
_ISSUE_NUMBER = st.integers(min_value=1, max_value=99999)

#: Atlassian-ish account-id strings (fits the loop-guard predicates).
_ACCOUNT_ID_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=30,
)

#: Event types the Jira router knows how to start a workflow for.
_WORKFLOW_START_EVENTS: tuple[str, ...] = (
    "jira:issue_created",
    "jira:issue_assigned",
    "jira:issue_updated",
)

#: Event types that route() classifies as "ignored" (representative
#: subset - the property only needs *one* unsupported example per run).
_UNSUPPORTED_EVENTS: tuple[str, ...] = (
    "jira:issue_deleted",
    "jira:project_archived",
    "confluence:page_created",
    "garbage:event",
)


def _project_keys_with_capability() -> st.SearchStrategy[str]:
    return st.sampled_from(sorted(_PROJECT_KEYS_WITH_CAPABILITY.keys()))


@st.composite
def _happy_payload(draw: st.DrawFn) -> tuple[bytes, str, str, str]:
    """Generate a payload + signature that satisfies every pre-condition.

    Returns ``(raw_body, signature_header, expected_workflow_id,
    expected_department_id)``.
    """

    project_key: str = draw(_project_keys_with_capability())
    expected_dept = _PROJECT_KEYS_WITH_CAPABILITY[project_key]
    issue_number = draw(_ISSUE_NUMBER)
    issue_key = f"{project_key}-{issue_number}"
    bot_id = draw(st.sampled_from(_BOT_ACCOUNT_IDS))
    actor_id = draw(_ACCOUNT_ID_TEXT.filter(lambda x: x not in _BOT_ACCOUNT_IDS))
    event_type = draw(st.sampled_from(_WORKFLOW_START_EVENTS))

    payload: dict[str, Any] = {
        "webhookEvent": event_type,
        "user": {"accountId": actor_id},
        "issue": {
            "key": issue_key,
            "fields": {
                "project": {"key": project_key},
                "assignee": {"accountId": bot_id},
            },
        },
    }
    if event_type == "jira:issue_updated":
        # Updated events only start a workflow when the changelog says
        # the assignee changed *to* a bot.
        payload["changelog"] = {
            "items": [
                {"field": "assignee", "to": bot_id},
            ]
        }

    raw_body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(_TEST_SECRET, raw_body, hashlib.sha256).hexdigest()
    signature = f"sha256={digest}"
    workflow_id = automation_workflow_id_jira(issue_key)
    return raw_body, signature, workflow_id, expected_dept


# ---- Failure mode strategies ----------------------------------------------

#: An enumeration of the pre-condition failures covered here.
_FAILURE_MODES: tuple[str, ...] = (
    "bad_signature",  # (1) HMAC fails  401 unauthorized
    "duplicate",  # (2) hash already in processed_events  200 duplicate
    "self_actor",  # (3) actor is a bot  200 loop_guard
    "unsupported_event",  # (4) event type ignored  200 ignored
    "non_bot_assignee",  # (5) assignee not a bot  200 not_bot_assignee
    "no_assignee_change",  # (5) issue_updated w/o assigneebot edge
    "missing_capability_no_dept",  # (6) project_key not mapped  missing_capability
    "missing_capability_no_cred",  # (6) dept exists but no jira cred  missing_capability
)


@st.composite
def _failing_payload(  # noqa: PLR0912 - one branch per failure mode
    draw: st.DrawFn,
) -> tuple[bytes, str, str]:
    """Generate a payload + signature that fails *exactly one* pre-condition.

    Returns ``(raw_body, signature_header, expected_status_string)``
    where ``expected_status_string`` is the value of the JSON ``status``
    field the handler returns (or ``"unauthorized"`` for the 401 leg).
    """

    mode = draw(st.sampled_from(_FAILURE_MODES))

    # Start from a baseline "would-be-happy" payload, then mutate.
    project_key = draw(_project_keys_with_capability())
    issue_number = draw(_ISSUE_NUMBER)
    issue_key = f"{project_key}-{issue_number}"
    bot_id = _BOT_ACCOUNT_IDS[0]
    non_bot_actor = draw(
        _ACCOUNT_ID_TEXT.filter(lambda x: x not in _BOT_ACCOUNT_IDS)
    )

    event_type: str = "jira:issue_created"
    actor_id: str = non_bot_actor
    assignee_id: str | None = bot_id
    changelog: dict[str, Any] | None = None
    use_correct_secret = True
    sign_with: bytes = _TEST_SECRET

    expected_status: str

    if mode == "bad_signature":
        use_correct_secret = False
        sign_with = b"wrong-secret-xyz"
        expected_status = "unauthorized"
    elif mode == "duplicate":
        # Caller will pre-seed the processed_events store with this
        # payload's hash; the handler then returns 200 duplicate.
        expected_status = "duplicate"
    elif mode == "self_actor":
        actor_id = bot_id
        expected_status = "loop_guard"
    elif mode == "unsupported_event":
        event_type = draw(st.sampled_from(_UNSUPPORTED_EVENTS))
        expected_status = "ignored"
    elif mode == "non_bot_assignee":
        # Use issue_created/issue_assigned with a non-bot assignee.
        event_type = draw(
            st.sampled_from(("jira:issue_created", "jira:issue_assigned"))
        )
        assignee_id = draw(
            _ACCOUNT_ID_TEXT.filter(lambda x: x not in _BOT_ACCOUNT_IDS)
        )
        expected_status = "not_bot_assignee"
    elif mode == "no_assignee_change":
        # issue_updated with a changelog that does NOT change to a bot.
        event_type = "jira:issue_updated"
        # Changelog with a non-bot assignee target, or a non-assignee
        # field. Hypothesis chooses.
        sub = draw(st.sampled_from(("non_bot_to", "non_assignee_field", "empty_items")))
        if sub == "non_bot_to":
            other_id = draw(
                _ACCOUNT_ID_TEXT.filter(lambda x: x not in _BOT_ACCOUNT_IDS)
            )
            changelog = {"items": [{"field": "assignee", "to": other_id}]}
        elif sub == "non_assignee_field":
            changelog = {
                "items": [{"field": "status", "to": "In Progress"}]
            }
        else:
            changelog = {"items": []}
        expected_status = "not_bot_assignee"
    elif mode == "missing_capability_no_dept":
        # Use a project_key that is NOT in any of our mapping tables.
        project_key = "UNMAPPED"
        issue_key = f"{project_key}-{issue_number}"
        expected_status = "missing_capability"
    elif mode == "missing_capability_no_cred":
        project_key = next(iter(_PROJECT_KEYS_DEPT_NO_CRED))
        issue_key = f"{project_key}-{issue_number}"
        expected_status = "missing_capability"
    else:  # pragma: no cover - exhaustive above
        raise AssertionError(f"unhandled failure mode {mode!r}")

    payload: dict[str, Any] = {
        "webhookEvent": event_type,
        "user": {"accountId": actor_id},
        "issue": {
            "key": issue_key,
            "fields": {
                "project": {"key": project_key},
                "assignee": (
                    {"accountId": assignee_id}
                    if assignee_id is not None
                    else None
                ),
            },
        },
    }
    if changelog is not None:
        payload["changelog"] = changelog
    elif event_type == "jira:issue_updated":
        # Default issue_updated changelog (assigneebot) when the failure
        # mode isn't specifically about assignee changes.
        payload["changelog"] = {
            "items": [{"field": "assignee", "to": bot_id}]
        }

    raw_body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(sign_with, raw_body, hashlib.sha256).hexdigest()
    signature = f"sha256={digest}"
    if use_correct_secret:
        # Sanity check (kept as comment): signature is valid.
        pass
    return raw_body, signature, expected_status


# ===========================================================================
# Helper: drive a request through the in-memory ASGI app
# ===========================================================================


async def _post_webhook(
    app: FastAPI, raw_body: bytes, signature: str
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post(
            "/webhooks/jira",
            content=raw_body,
            headers={
                "X-Hub-Signature": signature,
                "Content-Type": "application/json",
            },
        )


def _make_resolver() -> _FakeCredentialResolver:
    return _FakeCredentialResolver(
        [_FakeBotRow(service="jira", account_id=bid) for bid in _BOT_ACCOUNT_IDS]
    )


# ===========================================================================
# Happy-path post-condition
# ===========================================================================


class TestWebhookHappyPathPostCondition:
    """All six pre-conditions satisfied  the four post-conditions hold."""

    @_PROFILE
    @given(_happy_payload())
    @pytest.mark.asyncio
    async def test_post_state_matches_specification(
        self, generated: tuple[bytes, str, str, str]
    ) -> None:
        """Every fully-passing payload produces exactly the four
        post-condition artifacts.
        """
        raw_body, signature, workflow_id, expected_dept = generated

        store = _fresh_store()
        pool = _FakePool(store)
        temporal = _FakeTemporalClient()
        creds = _make_resolver()
        app = _build_test_app(pool=pool, temporal=temporal, creds=creds)

        resp = await _post_webhook(app, raw_body, signature)

        # (post 4) HTTP status + body shape.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"status": "accepted", "workflow_id": workflow_id}

        # (post 1) Exactly one processed_events row keyed by the canonical
        # SHA-256 hash of the payload.
        canonical = json.dumps(
            json.loads(raw_body),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        expected_hash = hashlib.sha256(canonical).hexdigest()
        assert store.processed_events == {expected_hash}

        # (post 2) Exactly one work_items row in 'pending'.
        assert len(store.work_items) == 1
        wi = store.work_items[0]
        assert wi.workflow_id == workflow_id
        assert wi.status == "pending"
        assert wi.department_id == expected_dept
        # Issue key is the suffix of the workflow_id by construction.
        assert wi.issue_key == workflow_id[len("automation-jira-") :]

        # (post 3) Exactly one Temporal workflow start with the same id.
        assert len(temporal.start_calls) == 1
        call = temporal.start_calls[0]
        assert call.workflow_type == "AutomationWorkflow"
        assert call.workflow_id == workflow_id
        assert call.task_queue == "automation-tq"


# ===========================================================================
# Failing pre-condition leaves work_items empty
# ===========================================================================


class TestWebhookFailingPreconditionPostCondition:
    """Any single failing pre-condition  0 work_items, 0 workflow starts."""

    @_PROFILE
    @given(_failing_payload())
    @pytest.mark.asyncio
    async def test_no_work_item_on_precondition_failure(
        self, generated: tuple[bytes, str, str]
    ) -> None:
        """For payloads that fail any of pre-conditions 1-6, the handler
        returns one of the alternative outcomes and inserts *zero*
        ``work_items`` rows. (Note: ``processed_events`` may still have
        a row inserted for failures that occur *after* the replay
        guard, e.g. self-actor or non-bot-assignee. The post-condition
        we care about for failures is the absence of work_items and
        workflow starts.)
        """
        raw_body, signature, expected_status = generated

        store = _fresh_store()

        # The "duplicate" failure mode requires the payload's hash to
        # already exist in processed_events before the request.
        if expected_status == "duplicate":
            canonical = json.dumps(
                json.loads(raw_body),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            store.processed_events.add(hashlib.sha256(canonical).hexdigest())

        pool = _FakePool(store)
        temporal = _FakeTemporalClient()
        creds = _make_resolver()
        app = _build_test_app(pool=pool, temporal=temporal, creds=creds)

        resp = await _post_webhook(app, raw_body, signature)

        # No work_item insert on any failure leg.
        assert store.work_items == []
        # No Temporal workflow start on any failure leg.
        assert temporal.start_calls == []

        # Response status code matches the alternative outcome class.
        if expected_status == "unauthorized":
            assert resp.status_code == 401
            assert resp.json() == {"status": "unauthorized"}
        else:
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body.get("status") == expected_status


# ===========================================================================
# Idempotence under exact replay
# ===========================================================================


class TestWebhookIdempotence:
    """A second delivery of the same payload is a no-op (replay guard)."""

    @_PROFILE
    @given(_happy_payload())
    @pytest.mark.asyncio
    async def test_replay_does_not_double_insert(
        self, generated: tuple[bytes, str, str, str]
    ) -> None:
        """After a happy-path 202, posting the same payload again returns
        ``200 status:duplicate`` and leaves the work_items table /
        Temporal start counter unchanged.
        """
        raw_body, signature, workflow_id, _expected_dept = generated

        store = _fresh_store()
        pool = _FakePool(store)
        temporal = _FakeTemporalClient()
        creds = _make_resolver()
        app = _build_test_app(pool=pool, temporal=temporal, creds=creds)

        first = await _post_webhook(app, raw_body, signature)
        assert first.status_code == 200
        assert first.json() == {"status": "accepted", "workflow_id": workflow_id}
        assert len(store.work_items) == 1
        assert len(temporal.start_calls) == 1

        second = await _post_webhook(app, raw_body, signature)
        assert second.status_code == 200
        assert second.json().get("status") == "duplicate"

        # Post-state is unchanged from the first delivery.
        assert len(store.work_items) == 1
        assert len(temporal.start_calls) == 1
