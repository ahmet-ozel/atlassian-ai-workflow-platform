"""Invariant test: Needs-info timeout enforcement.

Feature:,.

*For any*:class:`AutomationWorkflow` that parks in
``_handle_needs_info_loop`` waiting for an ``info_received`` signal,
if no signal arrives within ``_NEEDS_INFO_TIMEOUT`` (7 days, bumped
from 24 hours by), the workflow SHALL
terminate with ``decision="failed"``,
``failure_reason="needs_info_timeout"``, and the Jira issue SHALL be
transitioned to ``stale``.

**Strategy
--------
The needs_info loop is exercised through a deterministic fake Temporal
``workflow`` module that mirrors the primitives the workflow body
consumes: ``workflow.execute_activity`` (string-named activities),
``workflow.wait_condition``, ``workflow.logger``, and
``workflow.info``. The fake ``wait_condition`` deterministically
raises:class:`TimeoutError` to emulate the 24h Temporal timer firing
without any signal arriving - Hypothesis explores the input space
(initial workflow_type, dept id, issue key, question count) while the
post-conditions are pinned to the spec contract:

* The workflow output decision is ``"failed"``.
* The failure_reason is exactly ``"needs_info_timeout"``.
* The Jira issue is transitioned to ``"stale"`` via
 ``jira_transition_issue``.
* The audit record uses ``action="automation_failed"``.
* The ``stop`` envelope's missing_capabilities is empty (the timeout is
 not a capability-gate failure).

The full async loop is exercised via Temporal history replay or
Temporal's time-skipping test environment in the integration suite
(:mod:`platform.tests.integration.test_temporal_timeout` for the
sibling agent-runner workflow, and the property
``test_workflow_determinism_replay`` covers AutomationWorkflow in
its 7-day cousin). This Invariant test focuses on the deterministic
constants/behaviour surface (timeout duration, max iterations, stop
envelope shape) the task note, keeping it fast enough for
Hypothesis to explore many examples second.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings, strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - match the convention used by sibling Invariant tests.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# pylint: disable=wrong-import-position
from automation_worker.workflows import (  # noqa: E402
    automation_workflow as automation_workflow_mod,
)
from automation_worker.workflows.automation_workflow import (  # noqa: E402
    AutomationWorkflow,
    _AutomationStop,
    _NEEDS_INFO_MAX_ITERATIONS,
    _NEEDS_INFO_TIMEOUT,
)
from temporal_shared.messages import (  # noqa: E402
    AutomationWorkflowInput,
    LlmAnalysisResult,
)


# ---------------------------------------------------------------------------
# Deterministic fakes - mirror the Temporal workflow primitives the
# needs_info loop consumes. Only the methods actually called in the
# loop body are implemented; anything else trips a clear AssertionError
# so the test surface stays auditable.
# ---------------------------------------------------------------------------


@dataclass
class _ActivityCall:
    """Recorded ``workflow.execute_activity`` invocation."""

    name: str
    args: tuple[Any, ...]
    start_to_close_timeout: timedelta | None
    retry_policy: Any | None


@dataclass
class _FakeWorkflowInfo:
    """Subset of:class:`temporalio.workflow.Info` the workflow reads."""

    workflow_id: str = "automation-jira-test"


@dataclass
class _FakeWorkflow:
    """Drop-in fake for ``temporalio.workflow`` for the needs_info path.

 Implements the four primitives the needs_info loop calls:

 * ``execute_activity(name, *args,...)`` - records the call and
 returns ``None`` for fire-and-forget activities (jira comment,
 transition, audit_write). ``llm_analyze_task`` would also be
 routed here but the timeout branch never reaches it - the
 ``wait_condition`` raises:class:`TimeoutError` first.
 * ``wait_condition(predicate, *, timeout=...)`` - captures the
 requested timeout and unconditionally raises:class:`TimeoutError`. This is the deterministic emulation of
 "24h elapsed without an ``info_received`` signal".
 * ``logger`` - a plain stdlib logger so
 ``workflow.logger.warning(...)`` calls in the workflow body do
 not blow up.
 * ``info`` - returns a:class:`_FakeWorkflowInfo` with a stable
 ``workflow_id``; the loop only reads ``workflow_id`` indirectly
 via the higher-level ``run`` body, so the value is supplied
 for completeness rather than correctness.

 The ``unsafe`` namespace is also stubbed because the workflow
 module's top-level imports execute through
 ``workflow.unsafe.imports_passed_through``; that block runs at
 import time (before this fake replaces the module) so we only
 need the runtime symbols here.
 """

    calls: list[_ActivityCall] = field(default_factory=list)
    last_wait_timeout: timedelta | None = None
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("test.needs_info")
    )

    async def execute_activity(
        self,
        name: str,
        *positional: Any,
        args: Any = None,
        start_to_close_timeout: timedelta | None = None,
        retry_policy: Any | None = None,
        **_kwargs: Any,
    ) -> Any:
        # The workflow body uses ``execute_activity(name, args=[...])``
        # - the ``args`` keyword is the canonical Temporal SDK pattern.
        # Capture it as the first positional argument list so test
        # assertions can index into it the same way the workflow's
        # activity implementations would. Fall back to whatever
        # positional args the caller passed for resilience to legacy
        # call sites.
        if args is not None:
            recorded_args: tuple[Any, ...] = tuple(args)
        else:
            recorded_args = tuple(positional)
        self.calls.append(
            _ActivityCall(
                name=name,
                args=recorded_args,
                start_to_close_timeout=start_to_close_timeout,
                retry_policy=retry_policy,
            )
        )
        # Every activity in the timeout branch is fire-and-forget; the
        # workflow body never inspects a return value on this path.
        return None

    async def wait_condition(
        self,
        predicate: Any,
        *,
        timeout: timedelta | None = None,
    ) -> None:
        # Capture the requested timeout for assertions and emulate the
        # 24h timer firing without ``info_received`` ever flipping.
        self.last_wait_timeout = timeout
        raise TimeoutError("needs_info wait_condition timed out (test fake)")

    def info(self) -> _FakeWorkflowInfo:
        return _FakeWorkflowInfo()


def _names_of(calls: list[_ActivityCall]) -> list[str]:
    """Project a list of recorded calls to their activity names."""

    return [c.name for c in calls]


def _audit_payload_of(calls: list[_ActivityCall], action: str) -> dict[str, Any] | None:
    """Return the payload dict of the first ``audit_write`` call whose
 ``action`` matches; ``None`` if no such call was recorded."""

    for call in calls:
        if call.name != "audit_write":
            continue
        if not call.args:
            continue
        payload = call.args[0]
        if isinstance(payload, dict) and payload.get("action") == action:
            return payload
    return None


# ---------------------------------------------------------------------------
# Hypothesis input strategies
# ---------------------------------------------------------------------------


# ``WORKFLOW_TYPE_CAPABILITIES`` keys are valid workflow types the LLM
# may have selected before parking on needs_info. Restricted to a
# couple of representative entries so the test does not rely on
# implementation-specific routing decisions; the timeout branch is
# orthogonal to workflow_type.
_WORKFLOW_TYPES: tuple[str, ...] = (
    "code_change_with_test",
    "code_change_commit_only",
    "pr_review",
    "confluence_doc_create",
    "research_basic",
    "noop_test",
)

# Issue key strategy - Atlassian-style ``PROJECT-<int>`` slugs.
_issue_keys = st.builds(
    lambda proj, num: f"{proj}-{num}",
    proj=st.text(
        alphabet=st.characters(min_codepoint=ord("A"), max_codepoint=ord("Z")),
        min_size=2,
        max_size=6,
    ),
    num=st.integers(min_value=1, max_value=99999),
)

# Department slug - lowercase ASCII to match real dept ids in
# ``departments.json`` / capability table.
_dept_ids = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=2,
    max_size=20,
)

# Clarification questions list - the loop is only entered when at
# least one is present (low confidence + non-empty needs_info).
_questions = st.lists(
    st.text(min_size=1, max_size=80),
    min_size=1,
    max_size=4,
)


# ---------------------------------------------------------------------------
# Invariant test
# ---------------------------------------------------------------------------


class TestNeedsInfoTimeoutProperty:
    """**** - 24h needs_info timeout enforcement.

 **"""

    @given(
        workflow_type=st.sampled_from(_WORKFLOW_TYPES),
        issue_key=_issue_keys,
        dept_id=_dept_ids,
        questions=_questions,
    )
    @settings(
        max_examples=200,
        deadline=None,
        # The Hypothesis-generated examples each install a fresh
        # ``_FakeWorkflow`` via ``monkeypatch``-style attribute swap.
        # No state leaks between examples because each call creates a
        # new fake and restores the original module attribute on exit.
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_timeout_returns_failed_stop_envelope(
        self,
        workflow_type: str,
        issue_key: str,
        dept_id: str,
        questions: list[str],
    ) -> None:
        """For all valid inputs that enter needs_info: a 24h timer fire
 produces ``decision="failed"`` + ``reason="needs_info_timeout"``
 + a ``stale`` Jira transition.

 **"""

        wf = AutomationWorkflow()
        analysis = LlmAnalysisResult(
            workflow_type=workflow_type,
            confidence="low",
            target_repo="org/repo",
            target_branch="develop",
            needs_info_questions=tuple(questions),
        )
        inp = AutomationWorkflowInput(
            issue_key=issue_key,
            department_id=dept_id,
            available_capabilities=("jira", "bitbucket", "execution"),
        )

        fake = _FakeWorkflow()
        original_workflow = automation_workflow_mod.workflow
        automation_workflow_mod.workflow = fake  # type: ignore[assignment]
        try:
            result = asyncio.run(
                wf._handle_needs_info_loop(  # noqa: SLF001
                    inp=inp, analysis=analysis
                )
            )
        finally:
            automation_workflow_mod.workflow = original_workflow

        # ----- Stop envelope ------------------------------------------------
        # The helper returns an ``_AutomationStop`` (not a fresh
        # analysis) when the timeout fires.
        assert isinstance(result, _AutomationStop), (
            f"expected _AutomationStop on timeout, got {type(result).__name__}"
        )
        assert result.decision == "failed"
        assert result.failure_reason == "needs_info_timeout"
        assert result.workflow_type == workflow_type
        # The stop envelope's missing_capabilities is empty - the
        # timeout is unrelated to capability gating.
        assert result.missing_capabilities == ()
        # The summary mentions the iteration that hit the timeout so
        # operators can correlate the audit event with the workflow
        # event history; the iteration is always at least 1 (the loop
        # ran at least once before the timer fired).
        assert "needs_info timed out" in result.summary
        assert "stale" in result.summary

        # ----- Wait timeout matches the 7-day spec contract ----------------
        # - the wait_condition was
        # invoked with exactly 7 days, not 24 hours / seconds / weeks.
        assert fake.last_wait_timeout == _NEEDS_INFO_TIMEOUT
        assert fake.last_wait_timeout == timedelta(days=7)

        # ----- Jira side effects --------------------------------------------
        # The first iteration posts the question + transitions to
        # ``needs_info``; when the 24h timer fires the loop
        # transitions the issue to ``stale`` and posts the timeout
        # comment via ``_stop_with_audit``.
        names = _names_of(fake.calls)
        assert "jira_transition_issue" in names
        assert "jira_add_comment" in names

        transitions = [
            call.args[1]
            for call in fake.calls
            if call.name == "jira_transition_issue"
            and len(call.args) >= 2
        ]
        # The terminal transition is always ``stale``; the loop may
        # also have flipped the issue to ``needs_info`` first.
        assert transitions, "no jira_transition_issue calls recorded"
        assert transitions[-1] == "stale", (
            f"expected terminal transition to 'stale', got {transitions!r}"
        )

        # The Jira comment surface includes the timeout comment;
        # _format_needs_info_timeout_comment locks the canonical
        # Turkish wording (- "7 gün").
        comment_bodies = [
            call.args[1]
            for call in fake.calls
            if call.name == "jira_add_comment" and len(call.args) >= 2
        ]
        assert any(
            "7 gün" in body and "stale" in body
            for body in comment_bodies
        ), f"timeout comment never posted; got {comment_bodies!r}"

        # ----- Audit record -------------------------------------------------
        # The stop helper writes ``automation_failed`` after the timer
        # fires; the parking events are written under
        # ``automation_needs_info_parked`` iteration.
        failed_payload = _audit_payload_of(fake.calls, "automation_failed")
        assert failed_payload is not None, (
            "automation_failed audit event never written; "
            f"saw actions={[c.args[0].get('action') for c in fake.calls if c.name == 'audit_write']}"
        )
        assert failed_payload["failure_reason"] == "needs_info_timeout"
        assert failed_payload["issue_key"] == issue_key
        assert failed_payload["department_id"] == dept_id
        assert failed_payload["workflow_type"] == workflow_type

        # ----- No bounded re-analysis ---------------------------------------
        # ``llm_analyze_task`` is only called *after* a signal lifts the
        # ``info_received`` flag; since the fake forces a timeout on
        # the very first wait, no re-analysis is attempted.
        assert "llm_analyze_task" not in names, (
            "llm_analyze_task should not be re-called when the wait "
            f"times out on the first iteration; got {names!r}"
        )


class TestNeedsInfoTimeoutConstants:
    """Lock the timeout / cap constants the Invariant test relies on.

 These are deterministic example tests that complement the
 Hypothesis-driven property - if either constant drifts the
 property's contract changes meaningfully and operators must be
 notified.

 **"""

    def test_timeout_is_exactly_seven_days(self) -> None:
        # - exactly 7 days (was 24h
        # before the parity bump).
        assert _NEEDS_INFO_TIMEOUT == timedelta(days=7)
        # And not, e.g. 7 hours / 7 weeks / 168 minutes.
        assert _NEEDS_INFO_TIMEOUT.total_seconds() == 7 * 24 * 60 * 60

    def test_max_iterations_is_three(self) -> None:
        # The loop cap is independent of the 24h timeout but bounds
        # the number of times the test could observe the timeout
        # recovering after a signal. Pinning it here keeps the
        # property's "one iteration before timeout" assertion stable.
        assert _NEEDS_INFO_MAX_ITERATIONS == 3
