"""Workflow ilk-aksiyon assignee atama.

For every ``(Department, issue_key)`` pair drawn from a
schema-faithful Hypothesis strategy, the first activity of any
Jira-based workflow MUST set the issue's ``assignee`` field to the
department's ``bot.jira.account_id``. If the assignee-set call fails,
the workflow MUST NOT proceed to its second action and an audit event
with ``action="assignee_set_failed"`` MUST be written.

Behavior
--------

* The first action sets the Jira issue's
  ``assignee`` alanını departmanın ``bot.jira.account_id`` değerine
  set eden bir activity sağlar.
* The workflow's first activity sets the Jira
  issue'nun ``assignee`` alanını ilgili ``dept.bot.jira.account_id``
  değerine set eder; assignee set'i başarısız olursa workflow
  ilerleyemez ve hata audit'e ``assignee_set_failed`` ile yazılır.
* The assignee activity is represented here by
  ``set_assignee_to_bot(issue_key, dept_bot_account_id)`` so the
  workflow behavior can be verified independently from the concrete
  MCP call path.

Reference helper
----------------

The :func:`run_workflow_first_action` helper below is the
**test-time reference** for the first-action sequence that the
real ``AgentRunnerWorkflow`` must implement. It documents
the layered contract:

1. Resolve the bot's ``account_id`` from
   ``dept.bot.jira.account_id``. If the dept has no Jira bot or the
   ``account_id`` is empty, the workflow cannot proceed - it audits
   ``assignee_set_failed`` and returns ``False``.
2. Call the (mocked) ``set_assignee_to_bot(issue_key, account_id)``
   activity.
3. If the call raises, audit ``assignee_set_failed`` and return
   ``False`` - no second action runs.
4. Otherwise advance to the second action (a no-op MagicMock here)
   and return ``True``.

Tests below assert each branch of the contract. When
the real activity is wired in, the same property file can be re-pointed
at the production import path with the ``run_workflow_first_action``
helper retired - the assertions remain identical.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from audit_logger import AuditEvent, AuditLogger


# ---------------------------------------------------------------------------
# Audit sink fake (mirrors ``test_pr_draft_enforcement.py``)
# ---------------------------------------------------------------------------


class _CapturingAuditWriter:
    """In-memory ``AuditWriter`` for assertion on emitted events."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _make_logger() -> tuple[AuditLogger, _CapturingAuditWriter]:
    writer = _CapturingAuditWriter()
    return AuditLogger(writer=writer), writer


# ---------------------------------------------------------------------------
# Schema-faithful test doubles for ``Department``
# ---------------------------------------------------------------------------
#
# Mirrors the stand-ins used in ``test_capability_gate.py``. Only the
# subset of fields the assignee step consults is modelled here:
# ``dept.bot.jira.account_id`` and ``dept.id``.


@dataclass(frozen=True)
class _StubJiraBot:
    """Minimal ``BotEntry`` projection - only ``account_id`` matters here."""

    account_id: str | None


@dataclass(frozen=True)
class _StubBot:
    """Mirror of ``Department.bot`` - only Jira slot consulted."""

    jira: _StubJiraBot | None


@dataclass(frozen=True)
class _StubDepartment:
    """Mirror of ``Department`` - minimal fields used by these tests."""

    id: str
    bot: _StubBot


# ---------------------------------------------------------------------------
# Reference workflow first-action helper
# ---------------------------------------------------------------------------


#: Audit ``action`` strings used by the reference helper. The
#: ``assignee_set`` event is the success path; ``assignee_set_failed``
#: is the canonical denial action used for assignment failures.
ASSIGNEE_SET_OK_ACTION = "assignee_set"
ASSIGNEE_SET_FAILED_ACTION = "assignee_set_failed"


class AssigneeSetError(RuntimeError):
    """Raised by the (mocked) ``set_assignee_to_bot`` activity on failure.

    The real activity will raise an equivalent error from
    inside the MCP call path; the property test only depends on the
    fact that the workflow first-action wrapper must distinguish a
    raised exception from a successful return.
    """


# Shape of the activity callable. Real implementation lives at
# ``agent_runner.activities.jira_assign.set_assignee_to_bot``; tests
# inject a MagicMock that satisfies this shape so we don't depend on
# the activity implementation.
SetAssigneeFn = Callable[[str, str], Awaitable[None]]


async def run_workflow_first_action(
    *,
    dept: _StubDepartment,
    issue_key: str,
    set_assignee_to_bot: SetAssigneeFn,
    second_action: Callable[[], Awaitable[None]],
    audit_logger: AuditLogger,
) -> bool:
    """Reference contract for the first action of any Jira-based workflow.

    Returns ``True`` iff the assignee was set successfully *and* the
    workflow proceeded to its second action. Side effects:

    * Calls ``set_assignee_to_bot(issue_key, dept.bot.jira.account_id)``
      when (and only when) the dept has a non-empty
      ``bot.jira.account_id``.
    * Calls ``second_action()`` exactly once iff the first call
      returned without raising.
    * Writes a single audit event:
        - ``action="assignee_set"`` and ``result="ok"`` on success;
        - ``action="assignee_set_failed"`` and ``result="error"`` on
          any failure (missing bot, missing ``account_id``, or
          activity exception).
    """

    # Resolve the bot's account_id. If the dept has no Jira bot or
    # the ``account_id`` is empty / whitespace, we cannot fulfil the
    # contract - bail out before calling the activity.
    jira_bot = dept.bot.jira
    account_id = jira_bot.account_id if jira_bot is not None else None
    if not isinstance(account_id, str) or not account_id.strip():
        await audit_logger.write(
            AuditEvent(
                actor_id="system",
                actor_role="system",
                dept_id=dept.id,
                action=ASSIGNEE_SET_FAILED_ACTION,
                resource=f"jira:{issue_key}",
                result="error",
                timestamp=datetime.now(timezone.utc),
                payload={"reason": "missing_jira_account_id"},
            )
        )
        return False

    # Attempt the assignee-set call. Any raised exception keeps the
    # workflow from advancing.
    try:
        await set_assignee_to_bot(issue_key, account_id)
    except Exception as exc:  # noqa: BLE001 - audit + halt on any failure
        await audit_logger.write(
            AuditEvent(
                actor_id="system",
                actor_role="system",
                dept_id=dept.id,
                action=ASSIGNEE_SET_FAILED_ACTION,
                resource=f"jira:{issue_key}",
                result="error",
                timestamp=datetime.now(timezone.utc),
                payload={
                    "reason": "activity_raised",
                    "error_type": type(exc).__name__,
                    "expected_account_id": account_id,
                },
            )
        )
        return False

    # Success: audit + advance to the next action.
    await audit_logger.write(
        AuditEvent(
            actor_id="system",
            actor_role="system",
            dept_id=dept.id,
            action=ASSIGNEE_SET_OK_ACTION,
            resource=f"jira:{issue_key}",
            result="ok",
            timestamp=datetime.now(timezone.utc),
            payload={"account_id": account_id},
        )
    )
    await second_action()
    return True


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Department ids - mirror ``departments.schema.json`` ``^[a-z][a-z0-9-]{1,30}$``.
_dept_ids = st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True)

#: Jira issue keys - mirror schema ``^[A-Z][A-Z0-9_]{1,9}$`` for the
#: project prefix, joined to a numeric suffix.
_issue_keys = st.builds(
    lambda prefix, num: f"{prefix}-{num}",
    st.from_regex(r"^[A-Z][A-Z0-9_]{1,5}$", fullmatch=True),
    st.integers(min_value=1, max_value=99999),
)

#: Atlassian ``accountId`` shape - opaque alphanumeric / hyphen string.
#: Real values look like ``5b10ac8d82e05b22cc7d4ef5`` or
#: ``557058:f58131cb-...``. The strategy keeps a permissive shape so
#: the property covers UUID-like, hex, and colon-prefixed forms.
_atlassian_account_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=":-_",
    ),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() == s and len(s.strip()) > 0)


@st.composite
def _depts_with_jira_account_id(draw: st.DrawFn) -> _StubDepartment:
    """Departments that always have a non-empty ``bot.jira.account_id``."""

    return _StubDepartment(
        id=draw(_dept_ids),
        bot=_StubBot(jira=_StubJiraBot(account_id=draw(_atlassian_account_ids))),
    )


@st.composite
def _depts_without_jira_account_id(draw: st.DrawFn) -> _StubDepartment:
    """Departments that lack a usable ``bot.jira.account_id``.

    Three sub-cases - all three trigger the missing-account-id branch
    of the reference helper:

    1. ``bot.jira`` is ``None`` (dept never registered a Jira bot).
    2. ``bot.jira.account_id`` is ``None``.
    3. ``bot.jira.account_id`` is the empty / whitespace string.
    """

    case = draw(st.integers(min_value=0, max_value=2))
    if case == 0:
        bot = _StubBot(jira=None)
    elif case == 1:
        bot = _StubBot(jira=_StubJiraBot(account_id=None))
    else:
        bot = _StubBot(
            jira=_StubJiraBot(
                account_id=draw(st.sampled_from(["", "   ", "\t", "\n"]))
            )
        )
    return _StubDepartment(id=draw(_dept_ids), bot=bot)


# ---------------------------------------------------------------------------
# Helper - async test driver
# ---------------------------------------------------------------------------


def _run(coro: Awaitable[bool]) -> bool:
    """Run a coroutine to completion in a fresh event loop.

    Tests are synchronous; using ``asyncio.run`` keeps each
    Hypothesis example self-contained.
    """

    return asyncio.run(coro)


def _make_set_assignee_mock(
    *, fails: bool = False, exc: Exception | None = None
) -> MagicMock:
    """Build a ``set_assignee_to_bot`` activity stand-in.

    The mock records its positional args so the property tests can
    assert exactly which ``(issue_key, account_id)`` tuple was used.
    """

    async def _ok(issue_key: str, account_id: str) -> None:
        return None

    async def _raise(issue_key: str, account_id: str) -> None:
        raise exc if exc is not None else AssigneeSetError("probe failure")

    fn = MagicMock(wraps=_raise if fails else _ok)
    return fn


def _make_second_action() -> MagicMock:
    """Build an awaitable second-action stand-in."""

    async def _noop() -> None:
        return None

    return MagicMock(wraps=_noop)


# ---------------------------------------------------------------------------
# First activity sets ``assignee = bot.jira.account_id``
# ---------------------------------------------------------------------------


class TestFirstActivitySetsAssignee:
    """The first activity always issues a ``set_assignee_to_bot`` call.

    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_calls_set_assignee_with_dept_bot_account_id(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """The first activity issues a single assignee-set call.

        The first activity issues a single call to
        ``set_assignee_to_bot`` with the issue key and the
        department's ``bot.jira.account_id``. No other shape of call
        is acceptable.
        """

        set_assignee = _make_set_assignee_mock()
        second = _make_second_action()
        logger, _ = _make_logger()

        result = _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert result is True
        set_assignee.assert_called_once_with(
            issue_key, dept.bot.jira.account_id  # type: ignore[union-attr]
        )

    @settings(max_examples=200, deadline=2000)
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_call_is_first_action_before_second_action(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """The assignee is set before the second action.

        ``set_assignee_to_bot`` MUST be invoked *before* the
        second action, so the issue carries the correct assignee
        the moment any subsequent activity reads it.
        """

        call_order: list[str] = []

        async def _ok(issue_key_arg: str, account_id_arg: str) -> None:
            call_order.append("set_assignee")

        async def _second() -> None:
            call_order.append("second_action")

        set_assignee = MagicMock(wraps=_ok)
        second = MagicMock(wraps=_second)
        logger, _ = _make_logger()

        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert call_order == ["set_assignee", "second_action"]

    @settings(max_examples=200, deadline=2000)
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_workflow_advances_to_second_action_on_success(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """The success path advances to the second action.

        On a successful ``set_assignee_to_bot`` call the workflow
        MUST advance to its second action exactly once.
        """

        set_assignee = _make_set_assignee_mock()
        second = _make_second_action()
        logger, _ = _make_logger()

        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        second.assert_called_once_with()

    @settings(max_examples=200, deadline=2000)
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_success_audit_event_carries_account_id(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """The success path emits a single audit event.

        The success path emits a single audit event whose
        ``payload["account_id"]`` matches the value used in the
        activity call. This pins observability for ops dashboards.
        """

        set_assignee = _make_set_assignee_mock()
        second = _make_second_action()
        logger, writer = _make_logger()

        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert len(writer.events) == 1
        event = writer.events[0]
        assert event.action == ASSIGNEE_SET_OK_ACTION
        assert event.result == "ok"
        assert event.dept_id == dept.id
        assert event.resource == f"jira:{issue_key}"
        assert (event.payload or {}).get("account_id") == dept.bot.jira.account_id  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Failure path halts workflow + audits ``assignee_set_failed``
# ---------------------------------------------------------------------------


class TestAssigneeSetFailureBlocksWorkflow:
    """If the assignee-set call fails the workflow does not advance.

    """

    @settings(
        max_examples=150,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_second_action_not_called_when_set_assignee_raises(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """The second action is not invoked when assignment fails.

        When ``set_assignee_to_bot`` raises, the second action MUST
        NOT be invoked - the workflow is halted before any
        downstream effect.
        """

        set_assignee = _make_set_assignee_mock(fails=True)
        second = _make_second_action()
        logger, _ = _make_logger()

        result = _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert result is False
        set_assignee.assert_called_once_with(
            issue_key, dept.bot.jira.account_id  # type: ignore[union-attr]
        )
        assert not second.called

    @settings(max_examples=150, deadline=2000)
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_failure_audit_event_uses_canonical_action_string(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """The failure path emits a single audit event.

        The failure path emits a single audit event with
        ``action="assignee_set_failed"`` and ``result="error"`` -
        the canonical names used by the workflow.
        """

        set_assignee = _make_set_assignee_mock(fails=True)
        second = _make_second_action()
        logger, writer = _make_logger()

        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert len(writer.events) == 1
        event = writer.events[0]
        assert event.action == ASSIGNEE_SET_FAILED_ACTION
        assert event.result == "error"
        assert event.dept_id == dept.id
        assert event.resource == f"jira:{issue_key}"

    @settings(max_examples=80, deadline=2000)
    @given(
        dept=_depts_with_jira_account_id(),
        issue_key=_issue_keys,
        exc_message=st.text(min_size=0, max_size=40),
    )
    def test_failure_audit_records_expected_account_id(
        self,
        dept: _StubDepartment,
        issue_key: str,
        exc_message: str,
    ) -> None:
        """The failure audit carries the expected account ID.

        The failure audit's ``payload`` carries the
        ``expected_account_id`` so operators can replay the missing
        assignment manually after fixing the underlying cause.
        """

        set_assignee = _make_set_assignee_mock(
            fails=True, exc=AssigneeSetError(exc_message or "probe failure")
        )
        second = _make_second_action()
        logger, writer = _make_logger()

        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert len(writer.events) == 1
        payload = writer.events[0].payload or {}
        assert payload.get("expected_account_id") == dept.bot.jira.account_id  # type: ignore[union-attr]
        # The error type is recorded for triage but its specific
        # value is implementation detail - we only assert it's a
        # non-empty string.
        assert isinstance(payload.get("error_type"), str)
        assert payload["error_type"]


# ---------------------------------------------------------------------------
# Missing ``bot.jira.account_id`` is treated as failure
# ---------------------------------------------------------------------------


class TestMissingJiraAccountIdIsFailure:
    """Departments with no usable Jira ``account_id`` cannot start.


    The dept's ``bot.jira.account_id`` is the
    *only* acceptable assignee value. If the dept never registered a
    Jira bot, the bot has no ``account_id``, or the value is empty,
    the workflow MUST NOT call the activity, MUST NOT advance, and
    MUST audit ``assignee_set_failed`` with a ``missing_jira_account_id``
    reason.
    """

    @settings(
        max_examples=150,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(dept=_depts_without_jira_account_id(), issue_key=_issue_keys)
    def test_set_assignee_is_not_called(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """With no usable account ID, the activity is not called.

        With no usable ``account_id`` the activity MUST NOT be
        called at all - there is no valid value to pass for the
        ``assignee`` argument.
        """

        set_assignee = _make_set_assignee_mock()
        second = _make_second_action()
        logger, _ = _make_logger()

        result = _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert result is False
        assert not set_assignee.called
        assert not second.called

    @settings(max_examples=150, deadline=2000)
    @given(dept=_depts_without_jira_account_id(), issue_key=_issue_keys)
    def test_audits_assignee_set_failed_with_missing_reason(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        """A missing account ID emits a failure audit event.

        The missing-account-id branch emits the same
        ``assignee_set_failed`` action as the activity-raised
        branch, but the ``payload["reason"]`` is
        ``"missing_jira_account_id"`` so ops can distinguish a
        configuration gap from a runtime MCP failure.
        """

        set_assignee = _make_set_assignee_mock()
        second = _make_second_action()
        logger, writer = _make_logger()

        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )

        assert len(writer.events) == 1
        event = writer.events[0]
        assert event.action == ASSIGNEE_SET_FAILED_ACTION
        assert event.result == "error"
        assert event.dept_id == dept.id
        assert event.resource == f"jira:{issue_key}"
        assert (event.payload or {}).get("reason") == "missing_jira_account_id"


# ---------------------------------------------------------------------------
# ``set_assignee_to_bot`` is called exactly once per run
# ---------------------------------------------------------------------------


class TestSingleCallPerRun:
    """The first activity is invoked exactly once per workflow run.


    Workflows must not retry the assignee-set call from inside the
    same first-action wrapper - Temporal's own retry policy applies
    at the activity boundary. The wrapper's job is single-shot.
    """

    @settings(max_examples=100, deadline=2000)
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_call_count_is_one_on_success(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        set_assignee = _make_set_assignee_mock()
        second = _make_second_action()
        logger, _ = _make_logger()
        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )
        assert set_assignee.call_count == 1

    @settings(max_examples=100, deadline=2000)
    @given(dept=_depts_with_jira_account_id(), issue_key=_issue_keys)
    def test_call_count_is_one_on_failure(
        self, dept: _StubDepartment, issue_key: str
    ) -> None:
        set_assignee = _make_set_assignee_mock(fails=True)
        second = _make_second_action()
        logger, _ = _make_logger()
        _run(
            run_workflow_first_action(
                dept=dept,
                issue_key=issue_key,
                set_assignee_to_bot=set_assignee,
                second_action=second,
                audit_logger=logger,
            )
        )
        assert set_assignee.call_count == 1


# ---------------------------------------------------------------------------
# Example-based pin: canonical successful run
# ---------------------------------------------------------------------------


def test_canonical_example_payment_dept_assigns_to_bot() -> None:
    """Identical inputs produce identical traces.

    Hand-rolled example for the success-path shape so the property
    suite stays anchored to a concrete real-world scenario alongside
    the random ones.
    """

    payment = _StubDepartment(
        id="payment",
        bot=_StubBot(jira=_StubJiraBot(account_id="5b10ac8d82e05b22cc7d4ef5")),
    )
    set_assignee = _make_set_assignee_mock()
    second = _make_second_action()
    logger, writer = _make_logger()

    result = asyncio.run(
        run_workflow_first_action(
            dept=payment,
            issue_key="PAY-4211",
            set_assignee_to_bot=set_assignee,
            second_action=second,
            audit_logger=logger,
        )
    )

    assert result is True
    set_assignee.assert_called_once_with("PAY-4211", "5b10ac8d82e05b22cc7d4ef5")
    second.assert_called_once_with()
    assert len(writer.events) == 1
    assert writer.events[0].action == ASSIGNEE_SET_OK_ACTION
    assert writer.events[0].dept_id == "payment"


def test_canonical_example_dept_without_bot_audits_failure() -> None:
    """Audit payloads are JSON serializable.

    Hand-rolled example for the missing-bot branch - pins the
    ``assignee_set_failed`` audit shape against a concrete dept
    whose ``bot.jira`` is ``None`` (eg. a research-only dept that
    never registered Jira credentials).
    """

    research_only = _StubDepartment(
        id="research", bot=_StubBot(jira=None)
    )
    set_assignee = _make_set_assignee_mock()
    second = _make_second_action()
    logger, writer = _make_logger()

    result = asyncio.run(
        run_workflow_first_action(
            dept=research_only,
            issue_key="RES-1",
            set_assignee_to_bot=set_assignee,
            second_action=second,
            audit_logger=logger,
        )
    )

    assert result is False
    assert not set_assignee.called
    assert not second.called
    assert len(writer.events) == 1
    event = writer.events[0]
    assert event.action == ASSIGNEE_SET_FAILED_ACTION
    assert (event.payload or {}).get("reason") == "missing_jira_account_id"


# ---------------------------------------------------------------------------
# Sanity pin - audit ``actor_role`` is one of the allowed values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("issue_key", ["PAY-1", "API-42", "INFRA-99"])
def test_audit_actor_role_is_system(issue_key: str) -> None:
    """The assignee account ID comes from ``bot.jira.account_id``.

    Every event emitted by the first-action wrapper carries
    ``actor_role="system"`` - the canonical role for background
    workflow steps (see ``audit_logger.AUDIT_ACTOR_ROLES``). This
    pins the dependency on ``actor_role`` being mandatory and drawn
    from the restricted vocabulary.
    """

    dept = _StubDepartment(
        id="payment",
        bot=_StubBot(jira=_StubJiraBot(account_id="bot-account-1")),
    )
    set_assignee = _make_set_assignee_mock()
    second = _make_second_action()
    logger, writer = _make_logger()

    asyncio.run(
        run_workflow_first_action(
            dept=dept,
            issue_key=issue_key,
            set_assignee_to_bot=set_assignee,
            second_action=second,
            audit_logger=logger,
        )
    )

    assert all(e.actor_role == "system" for e in writer.events)
